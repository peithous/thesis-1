import copy

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.distributions as dist
from torch.nn.modules.rnn import RNNBase, RNNCellBase
from data import wrap
from torch.nn.init import orthogonal_ as orthogonal

def orthogonal_init(lstm, gain=1):
    """Orthogonal initialization of recurrent weights."""
    # Rewrote from Joost:
    # https://github.com/bastings/parser/blob/extended_parser/parser/utils.py
    lstm.reset_parameters()
    if isinstance(lstm, RNNBase):
        for _, weight_hh, _, _ in lstm.all_weights:
            for i in range(0, weight_hh.size(0), lstm.hidden_size):
                orthogonal(weight_hh[i:i + lstm.hidden_size], gain=gain)
    elif isinstance(lstm, RNNCellBase):
        for i in range(0, lstm.weight_hh.size(0), lstm.hidden_size):
            orthogonal(lstm.weight_hh[i:i + lstm.hidden_size], gain=gain)
    else:
        raise ValueError('what are you passing? A {}!?'.format(type(lstm)))


def bias_init(lstm, gain=1, forget_bias=1., rest_bias=0.):
    """Positive forget gate bias (Jozefowicz et al., 2015)."""
    # Rewrote from Joost:
    # https://github.com/bastings/parser/blob/extended_parser/parser/utils.py
    if isinstance(lstm, RNNBase):
        for _, _, bias_ih, bias_hh in lstm.all_weights:
            l = len(bias_ih)
            bias_ih.data.fill_(rest_bias)
            bias_hh.data.fill_(rest_bias)
            bias_ih[l // 4:l // 2].data.fill_(forget_bias)
            bias_hh[l // 4:l // 2].data.fill_(forget_bias)
    elif isinstance(lstm, RNNCellBase):
        l = len(lstm.bias_ih)
        lstm.bias_ih.data.fill_(rest_bias)
        lstm.bias_hh.data.fill_(rest_bias)
        lstm.bias_ih[l // 4:l // 2].data.fill_(forget_bias)
        lstm.bias_hh[l // 4:l // 2].data.fill_(forget_bias)
    else:
        raise ValueError('what are you passing? A {}!?'.format(type(lstm)))

def init_lstm(lstm, ortho=True, gain=1):
    """Initialize the forget bias and weights of LSTM."""
    if ortho:
        orthogonal_init(lstm, gain)
    bias_init(lstm)

class BiRecurrentEncoder(nn.Module):
    """A bidirectional RNN encoder for unpadded batches."""
    def __init__(self, input_size, hidden_size, num_layers, dropout, batch_first=True, device=None):
        super(BiRecurrentEncoder, self).__init__()
        self.fwd_rnn = nn.LSTM(input_size, hidden_size, num_layers,
                                batch_first=batch_first, dropout=dropout)
        self.bwd_rnn = nn.LSTM(input_size, hidden_size, num_layers,
                                batch_first=batch_first, dropout=dropout)
        self.device = device
        init_lstm(self.fwd_rnn)
        init_lstm(self.bwd_rnn)
        self.to(device)

    def _reverse(self, tensor):
        idx = [i for i in range(tensor.size(1) - 1, -1, -1)]
        idx = wrap(idx, device=self.device)
        return tensor.index_select(1, idx)

    def forward(self, x):
        """Forward pass works for unpadded, i.e. equal length, batches."""
        hf, _ = self.fwd_rnn(x)                 # [batch, seq, hidden_size]
        hb, _ = self.bwd_rnn(self._reverse(x))  # [batch, seq, hidden_size]

        # Select final representation.
        hf = hf[:, -1, :] # [batch, hidden_size]
        hb = hb[:, -1, :] # [batch, hidden_size]

        h = torch.cat((hf, hb), dim=-1) # [batch, 2*hidden_size]
        return h


class BaseLSTM(nn.Module):
    """A simple two-layered LSTM inherited by StackLSTM and HistoryLSTM."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(BaseLSTM, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size # Must be even number, see composition function.
        self.device = device # GPU or CPU

        self.rnn_1 = nn.LSTMCell(input_size, hidden_size)
        self.rnn_2 = nn.LSTMCell(hidden_size, hidden_size)

        # Were we store all intermediate computed hidden states.
        # Last item in _hidden_states_2 is used as the representation.
        self._hidden_states_1 = [] # layer 1
        self._hidden_states_2 = [] # layer 2

        # Used for custom dropout.
        self.keep_prob = 1.0 - dropout
        self.bernoulli = dist.Bernoulli(
                            probs=torch.tensor([self.keep_prob], device=device)
                        )
        init_lstm(self.rnn_1)
        init_lstm(self.rnn_2)
        self.initialize_hidden()
        self.to(device)

    def sample_recurrent_dropout_mask(self, batch_size):
        """Fix a new dropout mask used for recurrent dropout."""
        self._dropout_mask = self.bernoulli.sample(
                            (batch_size, self.hidden_size)
                        ).squeeze(-1)

    def dropout(self, x):
        """Custom recurrent dropout: same mask for the whole sequence."""
        scale = 1 / self.keep_prob # Scale the weights up to compensate for dropping out.
        return x * self._dropout_mask * scale

    def initialize_hidden(self, batch_size=1):
        """Set initial hidden state to zeros."""
        c = copy.deepcopy
        self._hidden_states_1 = []
        self._hidden_states_2 = []
        hx = Variable(torch.zeros(batch_size, self.hidden_size, device=self.device))
        cx = Variable(torch.zeros(batch_size, self.hidden_size, device=self.device))
        self.hx1, self.cx1 = hx, cx
        self.hx2, self.cx2 = c(hx), c(cx)
        self.sample_recurrent_dropout_mask(batch_size)

    def forward(self, x):
        """Compute the next hidden state with input x and the previous hidden state.

        Args:
            x (tensor): shape (batch, input_size).
        """
        # First layer
        self.hx1, self.cx1 = self.rnn_1(x, (self.hx1, self.cx1))
        if self.training:
            self.hx1, self.cx1 = self.dropout(self.hx1), self.dropout(self.cx1)
        # Second layer
        self.hx2, self.cx2 = self.rnn_2(self.hx1, (self.hx2, self.cx2))
        if self.training:
            self.hx2, self.cx2 = self.dropout(self.hx2), self.dropout(self.cx2)
        # Add cell states to memory.
        self._hidden_states_1.append((self.hx1, self.cx1))
        self._hidden_states_2.append((self.hx2, self.cx2))
        # Return hidden state of second layer
        return self.hx2


class StackLSTM(BaseLSTM):
    """A Stack-LSTM used to encode the stack of a transition based parser."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(StackLSTM, self).__init__(input_size, hidden_size, dropout, device)
        # BiRNN ecoder used as composition function.
        assert input_size % 2 == 0, 'input size must be even'
        self.composition = BiRecurrentEncoder(input_size, input_size//2, 2, dropout, device=device)

    def _reset_hidden(self, sequence_len):
        """Reset the hidden state to before opening the sequence."""
        self._hidden_states_1 = self._hidden_states_1[:-sequence_len]
        self._hidden_states_2 = self._hidden_states_2[:-sequence_len]
        self.hx1, self.cx1 = self._hidden_states_1[-1]
        self.hx2, self.cx2 = self._hidden_states_2[-1]

    def reduce(self, sequence):
        """Reduce a nonterminal sequence.

        Computes a BiRNN represesentation for the sequence, then replaces
        the reduced sequence of hidden states with this one representation.
        """
        # Length of sequence (minus extra nonterminal at end).
        length = sequence.size(1) - 1
        # Move hidden state back to before we opened the nonterminal.
        self._reset_hidden(length)
        # Return computed composition.
        return self.composition(sequence)


class HistoryLSTM(BaseLSTM):
    """An LSTM used to encode the history of actions of a transition based parser."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(HistoryLSTM, self).__init__(input_size, hidden_size, dropout, device)


class BufferLSTM(nn.Module):
    """A straightforward lstm but wrapped to hide internals such as selection of output."""
    def __init__(self, input_size, hidden_size, num_layers, dropout, device=None):
        super(BufferLSTM, self).__init__()
        self.rnn = nn.LSTM(input_size, hidden_size, dropout=dropout, num_layers=num_layers,
                           batch_first=True, bidirectional=False)

    def forward(self, x):
        """Encode and return the output hidden states."""
        h, _ = self.rnn(x)
        return h

if __name__ == '__main__':
    history_encoder = HistoryLSTM(2, 3, 0.1)
    init_lstm(history_encoder.rnn_1)


    buffer_encoder = BufferLSTM(2, 3, 2, 0.1)
    init_lstm(buffer_encoder.rnn)

    orthogonal_init(history_encoder.rnn_1)
