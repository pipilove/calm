import sys
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import rnn_cell

import code
import nn_impl


np.random.seed(666)


def _linear(args, output_size, bias, bias_start=0.0, scope=None):
  """Linear map: sum_i(args[i] * W[i]), where W[i] is a variable.
  Args:
    args: a 2D Tensor or a list of 2D, batch x n, Tensors.
    output_size: int, second dimension of W[i].
    bias: boolean, whether to add a bias term or not.
    bias_start: starting value to initialize the bias; 0 by default.
    scope: (optional) Variable scope to create parameters in.
  Returns:
    A 2D Tensor with shape [batch x output_size] equal to
    sum_i(args[i] * W[i]), where W[i]s are newly created matrices.
  Raises:
    ValueError: if some of the arguments has unspecified or wrong shape.
  """
  # Calculate the total size of arguments on dimension 1.
  total_arg_size = 0
  shapes = [a.get_shape() for a in args]
  for shape in shapes:
    if shape.ndims != 2:
      raise ValueError("linear is expecting 2D arguments: %s" % shapes)
    if shape[1].value is None:
      raise ValueError("linear expects shape[1] to be provided for shape %s, "
                       "but saw %d" % (shape, shape[1]))
    else:
      total_arg_size += shape[1].value

  dtype = [a.dtype for a in args][0]

  # Now the computation.
  scope = vs.get_variable_scope()
  with vs.variable_scope(scope) as outer_scope:
    weights = vs.get_variable(
        "weights", [total_arg_size, output_size], dtype=dtype)
    if len(args) == 1:
      res = tf.matmul(args[0], weights)
    else:
      res = tf.matmul(tf.concat(1, args), weights)
    if not bias:
      return res
    with vs.variable_scope(outer_scope) as inner_scope:
      inner_scope.set_partitioner(None)
      biases = vs.get_variable(
          "biases", [output_size],
          dtype=dtype,
          initializer=tf.constant_initializer(bias_start, dtype=dtype))
  return res + biases


class HyperCell(rnn_cell.RNNCell):

  def __init__(self, num_units, context_embed, mikolov_adapt=False, hyper_adapt=False):
    self._num_units = num_units
    self._forget_bias = 1.0
    self._activation = tf.tanh
    self.context_embed = context_embed
    self.mikolov_adapt = mikolov_adapt
    self.hyper_adapt = hyper_adapt

    with vs.variable_scope('hyper_lstm_cell'):
      if self.hyper_adapt:
        self.adaptation_weights = tf.get_variable(
          'adaptation_weights', 
          [context_embed.get_shape()[1].value, 3 * self._num_units])
        self.adaptation_bias = tf.get_variable(
          'adaptation_bias', [3 * self._num_units],
          initializer=tf.constant_initializer(np.ones(3 * self._num_units)))

      if self.mikolov_adapt:
        self.biases = tf.get_variable(
          'mikolov_biases', 
          [context_embed.get_shape()[1].value, 3 * self._num_units])

  @property
  def state_size(self):
    return rnn_cell.LSTMStateTuple(self._num_units, self._num_units)
    
  @property
  def output_size(self):
    return self._num_units

  def __call__(self, inputs, state, scope=None, reuse=None):
    with vs.variable_scope("hyper_lstm_cell", reuse=reuse):
      # Parameters of gates are concatenated into one multiply for efficiency.
      c, h = state
      adapted = _linear([inputs, h], 3 * self._num_units, True, scope=scope)

      if self.hyper_adapt:
        adaptation_coeff = (tf.matmul(self.context_embed, self.adaptation_weights)
                            + self.adaptation_bias)
        adapted = tf.mul(adaptation_coeff, adapted)
        
      if self.mikolov_adapt:
        delta = tf.matmul(self.context_embed, self.biases)
        adapted += delta

      # i = input_gate, j = new_input, f = forget_gate, o = output_gate
      j, f, o = tf.split(1, 3, adapted)
      forget_gate = tf.sigmoid(f + self._forget_bias)
      input_gate = 1.0 - forget_gate

      new_c = (c * forget_gate + input_gate * self._activation(j))
      new_h = self._activation(new_c) * tf.sigmoid(o)

      new_state = rnn_cell.LSTMStateTuple(new_c, new_h)
      return new_h, new_state


class BaseModel(object):
  """Hold the code that is shared between all model varients."""

  def __init__(self, params, unigram_probs, context_vocab_sizes=None):
    self.unigram_probs = unigram_probs
    self.max_length = params.max_len
    self.vocab_size = len(unigram_probs)
    self._embedding_dims = params.embedding_dims
    self.num_context_vars = len(context_vocab_sizes)
    self.x = tf.placeholder(tf.int32, [params.batch_size, self.max_length], name='x')
    self.y = tf.placeholder(tf.int64, [params.batch_size, self.max_length], name='y')
    self.seq_len = tf.placeholder(tf.int64, [params.batch_size], name='seq_len')

    enable_context_embeds = (params.use_mikolov_adaptation or params.use_hyper_adaptation 
                             or params.use_softmax_adaptation or params.use_hash_table)
    if enable_context_embeds:
      self.context_placeholders = {}
      self.context_embeddings = {}
      for i in range(self.num_context_vars):
        self.context_placeholders[params.context_vars[i]] = tf.placeholder(
          tf.int32, [None], name='context_var{0}'.format(i))
        self.context_embeddings[params.context_vars[i]] = tf.get_variable(
          'context_embedding{0}'.format(i), 
          [context_vocab_sizes[i], params.context_embed_sizes[i]])

      context_embeds = []
      for context_var in params.context_vars:
        context_embeds.append(tf.nn.embedding_lookup(
          self.context_embeddings[context_var],
          self.context_placeholders[context_var]))

      if len(context_embeds) == 1:
        self.final_context_embed = context_embeds[0]
      else:
        context_embeds = tf.concat(1, context_embeds)

        context_mlp = tf.get_variable(
          'context_mlp', [sum(params.context_embed_sizes), params.context_embed_size])
        context_bias = tf.get_variable('context_bias', [params.context_embed_size])
        
        self.final_context_embed = tf.nn.tanh(tf.matmul(context_embeds, context_mlp) + 
                                              context_bias)

    if params.use_softmax_adaptation:
      self._word_embeddings = tf.get_variable(
        'word_embeddings', [self.vocab_size, self._embedding_dims + params.context_embed_sizes[0]])
    else:
      self._word_embeddings = tf.get_variable(
        'word_embeddings', [self.vocab_size, self._embedding_dims])
      
    self._inputs = tf.nn.embedding_lookup(self._word_embeddings, self.x)

    if params.use_softmax_adaptation:
      self._inputs = self._inputs[:, :, params.context_embed_size:]

    self.base_bias = tf.get_variable('base_bias', [self.vocab_size])

    self.dropout_keep_prob = tf.placeholder_with_default(1.0, (), name='keep_prob')
    
    # make a mask
    indicator = tf.sequence_mask(tf.to_int32(self.seq_len - 1), self.max_length)
    sz = [params.batch_size, self.max_length]
    self._mask = tf.select(indicator, tf.ones(sz), tf.zeros(sz))

  def OutputHelper(self, reshaped_outputs, params, use_nce_loss=True, hash_func=None):
    if use_nce_loss:
      proj_out =  tf.reshape(reshaped_outputs, [self._mask.get_shape()[0].value,
                                                self._mask.get_shape()[1].value, -1])

      # add in the context embeddings
      if params.use_softmax_adaptation:
        packed_context_embed = tf.pack([self.final_context_embed] * params.max_len, 1)
        proj_out = tf.concat(2, [packed_context_embed, proj_out])

      if hash_func is None:
        losses = self.DoNCE(proj_out, self._word_embeddings, num_sampled=params.nce_samples)
      else:
        losses = self.AltNCE(proj_out, self._word_embeddings, params.nce_samples, hash_func)
                         
      masked_loss = tf.mul(losses, self._mask)
    else:
      # add in the context embeddings
      if params.use_softmax_adaptation:
        packed_context_embed = tf.pack([self.final_context_embed] * params.max_len, 1)
        reshaped_context = tf.reshape(packed_context_embed, [tf.shape(reshaped_outputs)[0], -1])
        reshaped_outputs = tf.concat(1, [reshaped_context, reshaped_outputs])

      masked_loss = self.ComputeLoss(reshaped_outputs, self._word_embeddings,
                                     hash_func=hash_func)
      self.masked_loss = masked_loss

    self.per_word_loss = tf.reshape(masked_loss, [-1, self.max_length])
    per_sentence_loss = tf.reduce_sum(self.per_word_loss, 1)
    self.per_sentence_loss = tf.div(per_sentence_loss, 
                                    tf.reduce_sum(self._mask, 1))

    self.cost = tf.reduce_sum(masked_loss) / tf.reduce_sum(self._mask)    


  def AltNCE(self, weights, out_embeddings, num_sampled, hash_func):
    losses = []
    w_unpack = tf.unpack(weights, axis=0)
    y_unpack = tf.unpack(self.y, axis=0)
    context_var = self.context_placeholders['subreddit']
    for idx, (w, y) in enumerate(zip(w_unpack, y_unpack)):
      c_val = context_var[idx]
      y_expanded = tf.expand_dims(y, 1)
      sampled_values = tf.nn.fixed_unigram_candidate_sampler(
        true_classes=y_expanded,
        num_true=1,
        num_sampled=num_sampled,
        unique=True,
        range_max=self.vocab_size,
        unigrams=self.unigram_probs)
      
      h_func = lambda(x): hash_func(x, c_val)

      nce_loss = nn_impl.sampled_softmax_loss(out_embeddings, self.base_bias,
                                              y_expanded, w, num_sampled, self.vocab_size,
                                              sampled_values=sampled_values,
                                              hash_func=h_func)
      losses.append(nce_loss)
    return tf.pack(losses, 0)

  def DoNCE(self, weights, out_embeddings, num_sampled=256):
    losses = []
    for w, y in zip(tf.unpack(weights, axis=1), tf.split(1, self.max_length, self.y)):
      sampled_values = tf.nn.fixed_unigram_candidate_sampler(
        true_classes=y,
        num_true=1,
        num_sampled=num_sampled,
        unique=True,
        range_max=self.vocab_size,
        unigrams=self.unigram_probs
      )
      nce_loss = tf.nn.sampled_softmax_loss(out_embeddings, self.base_bias, 
                                            w, y, num_sampled, self.vocab_size, 
                                            sampled_values=sampled_values)
                                          
      losses.append(nce_loss)
    return tf.pack(losses, 1)

  def ComputeLoss(self, reshaped_outputs, out_embeddings, user_embeddings=None,
                  hash_func=None):
    if user_embeddings is not None:
      replicated = tf.concat(0, [user_embeddings for _ in range(35)])
      reshaped_outputs = tf.concat(1, [replicated, reshaped_outputs])

    reshaped_mask = tf.reshape(self._mask, [-1])
    reshaped_labels = tf.reshape(self.y, [-1])

    bias = self.base_bias
    if hasattr(self, 'adapted_bias'):
      bias += self.adapted_bias
    reshaped_logits = tf.matmul(
      reshaped_outputs, out_embeddings, transpose_b=True) + bias

    if hash_func is not None:
      all_ids = tf.range(0, self.vocab_size)
      hash_vals = []
      for idx in range(self.x.get_shape()[0]):  # loop over batch_size
        s_id = self.context_placeholders['subreddit'][idx]
        hash_vals.append(hash_func(all_ids, s_id))
      hash_vals = tf.pack(hash_vals)
      expanded_hash_vals = tf.pack([hash_vals] * 35, 1)
      reshaped_hash_vals = tf.reshape(expanded_hash_vals, [-1, self.vocab_size])
      reshaped_logits += reshaped_hash_vals

    reshaped_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
      reshaped_logits, reshaped_labels)
    masked_loss = tf.mul(reshaped_loss, reshaped_mask)

    return masked_loss

  def CreateDecodingGraph(self, cell, params):

    # placeholders for decoder
    self.prev_word = tf.placeholder(tf.int32, (), name='prev_word')
    self.prev_c = tf.placeholder(tf.float32, [1, params.cell_size], name='prev_c')
    self.prev_h = tf.placeholder(tf.float32, [1, params.cell_size], name='prev_h')
    self.temperature = tf.placeholder_with_default([1.0], [1])

    # lookup embedding
    prev_embed = tf.nn.embedding_lookup(self._word_embeddings, self.prev_word)
    prev_embed = tf.expand_dims(prev_embed, 0)
    
    if params.use_softmax_adaptation:
      prev_embed = prev_embed[:, params.context_embed_size:]

    # one iteration of recurrent layer
    state = rnn_cell.LSTMStateTuple(self.prev_c, self.prev_h)
    with vs.variable_scope('RNN', reuse=True):
      result, (self.next_c, self.next_h) = cell(prev_embed, state)
      proj_result = tf.matmul(result, self.linear_proj)
      
    if params.use_softmax_adaptation:
      proj_result = tf.concat(1, [self.final_context_embed, proj_result])


    # softmax layer
    bias = self.base_bias
    if hasattr(params, 'use_hash_table') and params.use_hash_table:
      hval = self.hash_func(self.all_ids, self.context_placeholders['subreddit'])
      bias += hval
    logits = tf.matmul(proj_result, self._word_embeddings, transpose_b=True) + bias
    self.next_idx = tf.argmax(logits, 1)
    self.next_prob = tf.nn.softmax(logits / self.temperature)


class HyperModel(BaseModel):

  def __init__(self, params, unigram_probs, context_vocab_sizes, use_nce_loss=True):
    super(HyperModel, self).__init__(params, unigram_probs,
                                     context_vocab_sizes=context_vocab_sizes)

    self.hash_func = None  # setup the hash table
    if hasattr(params, 'use_hash_table') and params.use_hash_table:
      self.hash_func = self.GetHashFunc(params)

    context_embeds = None
    if params.use_mikolov_adaptation or params.use_hyper_adaptation:
      context_embeds = self.final_context_embed

    cell = HyperCell(params.cell_size, context_embeds,
                     mikolov_adapt=params.use_mikolov_adaptation,
                     hyper_adapt=params.use_hyper_adaptation)

    regularized_cell = rnn_cell.DropoutWrapper(
      cell, output_keep_prob=self.dropout_keep_prob,
      input_keep_prob=self.dropout_keep_prob)

    self.linear_proj = tf.get_variable(
      'linear_proj', [params.cell_size, params.embedding_dims])
    outputs, _ = tf.nn.dynamic_rnn(regularized_cell, self._inputs, dtype=tf.float32,
                                   sequence_length=self.seq_len)
    self.outputs = outputs
    reshaped_outputs = tf.reshape(outputs, [-1, params.cell_size])
    projected_outputs = tf.matmul(reshaped_outputs, self.linear_proj)
    self.OutputHelper(projected_outputs, params, use_nce_loss=use_nce_loss,
                      hash_func=self.hash_func)

    self.CreateDecodingGraph(cell, params)

  def GetHashFunc(self, params):
    """Returns a function that hashes context."""
    self.hash_table = tf.get_variable(
      'hash_table', [params.hash_table_size],
      initializer=tf.random_normal_initializer(0, 0.1))

    bloom_table_size = 100000007
    self.bloom_table = tf.Variable(trainable=False, dtype=tf.uint8, 
                                   initial_value=np.zeros(bloom_table_size))
    self.subreddit_id = tf.placeholder(tf.int32, (), name='subreddit_id')
    self.selected_ids = tf.placeholder(tf.int32, [None], name='selected_ids')

    w_hash_mat = tf.constant(np.random.randint(2**30, size=[1, 16]), 
                             dtype=tf.int32)
    s_hash_mat = tf.constant(np.random.randint(2**30, size=[1, 16]),
                             dtype=tf.int32)

    hw = tf.matmul(tf.expand_dims(self.selected_ids, 1), w_hash_mat)
    hs = tf.mul(self.subreddit_id, s_hash_mat)
    h = tf.abs(tf.mod(hw + hs, bloom_table_size))
    reshaped_h = tf.reshape(h, [-1])
    self.update_bloom = tf.scatter_update(
      self.bloom_table, reshaped_h, tf.ones_like(reshaped_h, dtype=tf.uint8))

    w_hash = tf.constant(np.random.randint(2**30), dtype=tf.int32)
    s_hash = tf.constant(np.random.randint(2**30), dtype=tf.int32)
    def GetHash(ids, s_id):
      ids = tf.to_int32(ids)

      # lookup entry in bloom table
      hw_bloom = tf.matmul(tf.expand_dims(ids, 1), w_hash_mat)
      hs_bloom = tf.squeeze(tf.mul(s_id, s_hash_mat))
      h_bloom = tf.abs(tf.mod(hw_bloom + hs_bloom, bloom_table_size))
      bloom_lookup = tf.nn.embedding_lookup(self.bloom_table, h_bloom)
      final_bloom = tf.to_float(tf.reduce_prod(bloom_lookup, 1))

      hw = tf.mul(ids, w_hash)
      hs = tf.mul(s_id, s_hash)
      h = tf.abs(tf.mod(hw + hs, params.hash_table_size))
      h_val = tf.nn.embedding_lookup(self.hash_table, h)
      
      filtered_h_val = tf.mul(final_bloom, h_val)
      return filtered_h_val

    self.all_ids = tf.range(0, self.vocab_size)
    self.sub_hash = GetHash(self.all_ids, self.subreddit_id)

    return GetHash
