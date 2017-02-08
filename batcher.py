import argparse
import bz2
import gzip
import itertools
import numpy as np
import random


def GetFileHandle(filename):
  if filename.endswith('.bz2'):
    return bz2.BZ2File(filename, 'r')
  if filename.endswith('.gz'):
    return gzip.open(filename, 'r')
  return open(filename, 'r')


def WordSplitter(text):
  return text.lower.split()


def CharSplitter(text):
  return list(text)


def ReadData(filename, limit=10000000, mode='train', worker=None,
             num_workers=None, splitter='word'):
  usernames = []
  texts = []

  SplitFunc = {'word': WordSplitter, 'char': CharSplitter}[splitter]

  with GetFileHandle(filename) as f:
    for idnum, line in enumerate(f):
      fields = line.split('\t')
      username = fields[0]
      text = fields[-1]

      if idnum % 100000 == 0:
        print idnum

      if idnum > limit:
        break

      if worker is not None and int(idnum) % num_workers != worker:
        continue

      if mode != 'all':
        if mode == 'train' and int(idnum) % 10 < 1:
          continue
        if mode != 'train' and int(idnum) % 10 >= 1:
          continue

      usernames.append(username)
      texts.append(['<S>'] + SplitFunc(text) + ['</S>'])

  return usernames, texts


class Dataset(object):

  def __init__(self, max_len=35, batch_size=100, preshuffle=True, name='unnamed'):
    """Init the dataset object.

    Args:
      batch_size: size of mini-batch
      preshuffle: should the order be scrambled before the first epoch
      name: optional name for the dataset
    """
    self._sentences = []
    self._usernames = []
    self.name = name

    self.batch_size = batch_size
    self.preshuffle = preshuffle
    self._max_len = max_len

  def AddDataSource(self, usernames, sentences):
    self._sentences.append(sentences)
    self._usernames.append(usernames)

  def GetSentences(self):
    return itertools.chain(*self._sentences)

  def Prepare(self, word_vocab, username_vocab):
    sentences = list(itertools.chain(*self._sentences))
  
    self.seq_lens = np.array([min(len(x), self._max_len) for x in sentences])
    
    self.current_idx = 0

    self.sentences = self.GetNumberLines(sentences, word_vocab,
                                         self._max_len)
    self.usernames = np.array([username_vocab[u] for u in 
                               itertools.chain(*self._usernames)])

    self.N = len(sentences)
    if self.preshuffle:
      self._Permute()


  @staticmethod
  def GetNumberLines(lines, vocab, pad_length):
    """Convert list of words to matrix of word ids."""
    out = []
    for line in lines:
      ids = [vocab[w] for w in line[:pad_length]]
      if len(ids) < pad_length:
        ids += [vocab['}']] * (pad_length - len(ids))
      out.append(ids)
    return np.array(out)

  def GetNumBatches(self):
    """Returns num batches per epoch."""
    return self.N / self.batch_size

  def _Permute(self):
    """Shuffle the training data."""
    s = np.arange(self.N)
    np.random.shuffle(s)

    self.sentences = self.sentences[s, :]
    self.seq_lens = self.seq_lens[s]
    self.usernames = self.usernames[s]

  def GetNextBatch(self):
    if self.current_idx + self.batch_size > self.N:
      self.current_idx = 0

      self._Permute()    

    idx = range(self.current_idx, self.current_idx + self.batch_size)
    self.current_idx += self.batch_size

    return (self.sentences[idx, :], self.seq_lens[idx], 
            self.usernames[idx])


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--filename', default='/s0/ajaech/reddit.tsv.bz2')
  parser.add_argument('--mode', choices=['train', 'eval'])
  parser.add_argument('--out')
  args = parser.parse_args()

  _, texts = ReadData(args.filename, mode=args.mode)
  with open(args.out, 'w') as f:
    for t in texts:
      f.write(' '.join(t[1:-1]))
      f.write('\n')
