import copy

import torch
import torch.nn as nn
import torch.distributions as dist

from data import wrap
from nn import init_lstm
from composition import BiRecurrentComposition, AttentionComposition, LatentFactorComposition


class BaseLSTM(nn.Module):
    """A simple two-layered LSTM inherited by StackLSTM and HistoryLSTM."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(BaseLSTM, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size  # Must be even number, see composition function.
        self.device = device  # GPU or CPU

        self.rnn_1 = nn.LSTMCell(input_size, hidden_size)
        self.rnn_2 = nn.LSTMCell(hidden_size, hidden_size)

        # Were we store all intermediate computed hidden states.
        # Last item in _hidden_states_2 is used as the representation.
        self._hidden_states_1 = []  # layer 1
        self._hidden_states_2 = []  # layer 2

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
        scale = 1 / self.keep_prob  # Scale the weights up to compensate for dropping out.
        return x * self._dropout_mask * scale

    def initialize_hidden(self, batch_size=1):
        """Set initial hidden state to zeros."""
        c = copy.deepcopy
        self._hidden_states_1 = []
        self._hidden_states_2 = []
        h0 = torch.zeros(batch_size, self.hidden_size, device=self.device)
        c0 = torch.zeros(batch_size, self.hidden_size, device=self.device)
        self.hx1, self.cx1 = h0, c0
        self.hx2, self.cx2 = c(h0), c(c0)
        self._hidden_states_1.append((self.hx1, self.cx1))
        self._hidden_states_2.append((self.hx2, self.cx2))
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
    def __init__(self, input_size, hidden_size, dropout, device=None, composition='basic'):
        super(StackLSTM, self).__init__(input_size, hidden_size, dropout, device)
        # Composition function.
        assert composition in (
            'basic', 'attention', 'latent-factors', 'latent-attention'), composition
        self.requires_kl = (composition in ('latent-factors', 'latent-attention'))
        if composition == 'attention':
            self.composition = AttentionComposition(input_size, 2, dropout, device=device)
        if composition == 'latent-factors':
            self.composition = LatentFactorComposition(20, input_size, 2, dropout, device=device)
        else:
            self.composition = BiRecurrentComposition(input_size, 2, dropout, device=device)

    def _reset_hidden(self, sequence_len):
        """Reset the hidden state to before opening the sequence."""
        del self._hidden_states_1[-sequence_len:], self._hidden_states_2[-sequence_len:]
        self.hx1, self.cx1 = self._hidden_states_1[-1]
        self.hx2, self.cx2 = self._hidden_states_2[-1]


class HistoryLSTM(BaseLSTM):
    """An LSTM used to encode the history of actions of a transition based parser."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(HistoryLSTM, self).__init__(
            input_size,
            hidden_size,
            dropout,
            device
        )


class TerminalLSTM(BaseLSTM):
    """An LSTM used to encode the history of actions of a transition based parser."""
    def __init__(self, input_size, hidden_size, dropout, device=None):
        super(TerminalLSTM, self).__init__(
            input_size,
            hidden_size,
            dropout,
            device
        )


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
    # init_lstm(history_encoder.rnn_1)

    for name, param in history_encoder.rnn_1.named_parameters():
        print(name)

    buffer_encoder = BufferLSTM(2, 3, 2, 0.1)
    init_lstm(buffer_encoder.rnn)
    for name, param in buffer_encoder.rnn.named_parameters():
        print(name)

    orthogonal_init(history_encoder.rnn_1)
