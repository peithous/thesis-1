import torch
import torch.nn as nn
from torch.autograd import Variable

from model import BiRecurrentEncoder

class BiRecurrentEncoder(nn.Module):
    """A bidirectional RNN encoder."""
    def __init__(self,input_size, hidden_size, num_layers, dropout, batch_first=True, cuda=False):
        super(BiRecurrentEncoder, self).__init__()
        self.forward_rnn = nn.LSTM(input_size=input_size, hidden_size=hidden_size,
                           num_layers=num_layers, batch_first=batch_first,
                           dropout=dropout)
        self.backward_rnn = nn.LSTM(input_size=input_size, hidden_size=hidden_size,
                           num_layers=num_layers, batch_first=batch_first,
                           dropout=dropout)
        self.cuda = cuda

    def _reverse(self, tensor):
        idx = [i for i in range(tensor.size(1) - 1, -1, -1)]
        idx = Variable(torch.LongTensor(idx))
        idx = idx.cuda() if self.cuda else idx
        return tensor.index_select(1, idx)

    def forward(self, x):
        hf, _ = self.forward_rnn(x)                 # [batch, seq, hidden_size]
        hb, _ = self.backward_rnn(self._reverse(x)) # [batch, seq, hidden_size]

        # select final representation
        hf = hf[:, -1, :] # [batch, hidden_size]
        hb = hb[:, -1, :] # [batch, hidden_size]

        h = torch.cat((hf, hb), dim=-1) # [batch, 2*hidden_size]
        return h


class StackLSTM(nn.Module):
    def __init__(self, input_size, hidden_size, cuda=False):
        super(StackLSTM, self).__init__()
        self.cuda = cuda
        self.input_size = input_size
        self.hidden_size = hidden_size

        self.rnn = nn.LSTMCell(input_size, hidden_size)
        # hidden_size//2 because the output of the composition function
        # is a concatenation of two hidden vectors.
        self.composition = BiRecurrentEncoder(input_size, hidden_size//2,
                                              num_layers=1, dropout=0.)

        self._hidden_states = []

        self.initialize_hidden()

    def _reset_hidden(self, sequence_len):
        """Reset the hidden state to before opening the sequence."""
        self._hidden_states = self._hidden_states[:-sequence_len]
        self.hx, self.cx = self._hidden_states[-1]

    def initialize_hidden(self, batch_size=1):
        """Set initial hidden state to zeros."""
        hx = Variable(torch.zeros(batch_size, self.hidden_size))
        cx = Variable(torch.zeros(batch_size, self.hidden_size))
        if self.cuda:
            hx = hx.cuda()
            cx = cx.cuda()
        self.hx, self.cx = hx, cx

    def reduce(self, sequence):
        """Computes a bidirectional rnn represesentation for the sequence"""
        length = sequence.size(1) - 1 # length of sequence (minus extra nonterminal at end)
        # Move hidden state back to before we opened the nonterminal.
        self._reset_hidden(length)
        return self.composition(sequence)

    def forward(self, x):
        # x is shape (batch, input_size)
        self.hx, self.cx = self.rnn(x, (self.hx, self.cx))
        self._hidden_states.append((self.hx, self.cx)) # add cell states to memory
        return self.hx