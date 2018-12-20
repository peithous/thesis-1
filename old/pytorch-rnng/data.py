import sys
import os
import string
from tqdm import tqdm

import torch
import numpy as np

from datatypes import Token, Word, Nonterminal, Action
from actions import SHIFT, REDUCE, NT, GEN
from data_scripts.get_oracle import unkify


PAD_CHAR = '_'
BASE_UNK_TOKEN = 'UNK'

PAD_INDEX = 0
BASE_UNK_INDEX = 1


def pad(batch):
    """Pad a batch of irregular length indices."""
    lens = list(map(len, batch))
    max_len = max(lens)
    padded_batch = []
    for k, seq in zip(lens, batch):
        padded =  seq + (max_len - k)*[PAD_INDEX]
        padded_batch.append(padded)
    return padded_batch


def wrap(batch, device):
    """Packages the batch as a Variable containing a LongTensor."""
    assert isinstance(batch, list)
    if len(batch) > 1 and isinstance(batch[0], list):
        batch = pad(batch)
    tensor = torch.tensor(batch, device=device, dtype=torch.long)
    return tensor.to(device)


def get_sentences(path):
    """Chunks the oracle file into sentences."""
    def get_sent_dict(sent):
        d = {
                'tree'     : sent[0],
                'tags'     : sent[1],
                'original' : sent[2],
                'lower'    : sent[3],
                'unked'    : sent[4],
                'actions'  : sent[5:]
            }
        return d

    sentences = []
    with open(path) as f:
        sent = []
        for line in f:
            if line == '\n':
                sentences.append(sent)
                sent = []
            else:
                sent.append(line.rstrip())
        return [get_sent_dict(sent) for sent in sentences if sent]


class Dictionary:
    """A dictionary for stack, buffer, and action symbols."""
    def __init__(self, path, name, use_chars=False):
        self.n2i = dict()  # nonterminals
        self.w2i = dict()  # words
        self.i2n = []
        self.i2w = []
        self.use_chars = use_chars
        self.initialize()
        self.read(path, name)

    def initialize(self):
        self.w2i[PAD_CHAR] = PAD_INDEX
        self.w2i[BASE_UNK_TOKEN] = BASE_UNK_INDEX
        self.i2w.append(PAD_CHAR)
        self.i2w.append(BASE_UNK_TOKEN)

    def read(self, path, name):
        with open(os.path.join(path, name + '.vocab'), 'r') as f:
            start = len(self.w2i)
            if self.use_chars:
                chars = set(f.read())
                printable = set(string.printable)
                chars = list(chars | printable)
                for i, w in enumerate(chars):
                    self.w2i[w] = i
                    self.i2w.append(w)
            else:
                for i, line in enumerate(f, start):
                    w = line.rstrip()
                    self.w2i[w] = i
                    self.i2w.append(w)
        with open(os.path.join(path, name + '.nonterminals'), 'r') as f:
            start = len(self.n2i)
            for i, line in enumerate(f, start):
                s = line.rstrip()
                self.n2i[s] = i
                self.i2n.append(s)

    @property
    def unks(self, unk_start=BASE_UNK_TOKEN):
        return [w for w in self.w2i if w.startswith(unk_start)]

    @property
    def num_words(self):
        return len(self.w2i)

    @property
    def num_nonterminals(self):
        return len(self.n2i)


class Data:
    """A dataset with parse configurations."""
    def __init__(self,
                 path,
                 dictionary,
                 model,
                 textline,
                 use_chars=False,
                 max_lines=-1):
        assert textline in ('original', 'lower', 'unked'), textline
        self.dictionary = dictionary
        self.sentences = []
        self.actions = []
        self.use_chars = use_chars
        self.model = model
        self.textline = textline
        self.read(path, max_lines)

    def __str__(self):
        return f'{len(self.sentences):,} sentences'

    def _order(self, new_order):
        self.sentences = [self.sentences[i] for i in new_order]
        self.actions = [self.actions[i] for i in new_order]

    def _process(self, token):
        assert isinstance(token, Token)
        if self.use_chars:
            index = [self.dictionary.w2i[char] for char in token.original]
        else:
            try:
                index = self.dictionary.w2i[token.processed]
            except KeyError:
                # Unkify the token.
                unked = unkify([token.original], self.dictionary.w2i)[0]
                # Maybe the unkified word is not in the dictionary.
                try:
                    index = self.dictionary.w2i[unked]
                except KeyError:
                    index = self.dictionary.w2i[BASE_UNK_TOKEN]
                    unked = BASE_UNK_TOKEN
                token = Token(token.original, unked)
        return token, index

    def _get_tokens(self, original, processed):
        assert all(isinstance(word, str) for word in original), original
        assert all(isinstance(word, str) for word in processed), processed
        sentence = [Token(orig, proc) for orig, proc in zip(original, processed)]
        sentence_items = []
        for token in sentence:
            token, index = self._process(token)
            sentence_items.append(Word(token, index))
        return sentence_items

    def _get_actions(self, sentence, actions):
        assert all(isinstance(action, str) for action in actions), actions
        assert all(isinstance(word, Word) for word in sentence), sentence
        action_items = []
        token_idx = 0
        for a in actions:
            if a == 'SHIFT':
                if self.model == 'disc':
                    action = Action('SHIFT', Action.SHIFT_INDEX)
                if self.model == 'gen':
                    word = sentence[token_idx]
                    action = GEN(Word(word, self.dictionary.w2i[word.token.processed]))
                    token_idx += 1
            elif a == 'REDUCE':
                action = Action('REDUCE', Action.REDUCE_INDEX)
            elif a.startswith('NT'):
                nt = a[3:-1]
                action = NT(Nonterminal(nt, self.dictionary.n2i[nt]))
            action_items.append(action)
        return action_items

    def read(self, path, max_lines):
        sents = get_sentences(path)  # a list of dictionaries
        for i, sent_dict in enumerate(tqdm(sents, file=sys.stdout)):
            if max_lines > 0 and i > max_lines:
                break
            original_tokens = sent_dict['original'].split()
            processed_tokens = sent_dict[self.textline].split()
            sentence = self._get_tokens(original_tokens, processed_tokens)
            actions = self._get_actions(sentence, sent_dict['actions'])
            self.sentences.append(sentence)
            self.actions.append(actions)
        self.lengths = [len(sent) for sent in self.sentences]

    def order(self):
        old_order = zip(range(len(self.lengths)), self.lengths)
        new_order, _ = zip(*sorted(old_order, key=lambda t: t[1]))
        self._order(new_order)

    def shuffle(self):
        n = len(self.sentences)
        new_order = list(range(0, n))
        np.random.shuffle(new_order)
        self._order(new_order)

    def batches(self, shuffle=False, length_ordered=False):
        n = len(self.sentences)
        if shuffle:
            self.shuffle()
        if length_ordered:
            self.order()
        batches = []
        for i in range(n):
            sentence = self.sentences[i]
            actions = self.actions[i]
            batches.append((sentence, actions))
        return batches


class Corpus:
    """A corpus of three datasets (train, development, and test) and a dictionary."""
    def __init__(self,
                 data_path='../data',
                 model='disc',
                 textline='unked',
                 name='ptb',
                 use_chars=False,
                 max_lines=-1):
        self.dictionary = Dictionary(
            path=os.path.join(data_path, 'vocab', textline),
            name=name,
            use_chars=use_chars)
        self.train = Data(
            path=os.path.join(data_path, 'train', name + '.train.oracle'),
            dictionary=self.dictionary,
            model=model,
            textline=textline,
            use_chars=use_chars,
            max_lines=max_lines)
        self.dev = Data(
            path=os.path.join(data_path, 'dev', name + '.dev.oracle'),
            dictionary=self.dictionary,
            model=model,
            textline=textline,
            use_chars=use_chars)
        self.test = Data(
            path=os.path.join(data_path, 'test', name + '.test.oracle'),
            dictionary=self.dictionary,
            model=model,
            textline=textline,
            use_chars=use_chars)

    def __str__(self):
        items = (
            'Corpus',
             f'vocab size: {self.dictionary.num_words:,}',
             f'train: {str(self.train)}',
             f'dev: {str(self.dev)}',
             f'test: {str(self.test)}',
        )
        return '\n'.join(items)