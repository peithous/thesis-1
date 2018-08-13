import logging
from typing import List, Tuple

import torch
import torch.nn as nn

from data_test import wrap
from datatypes import Item, Word, Nonterminal, Action
from actions import SHIFT, REDUCE, GEN, NT
from tree_test import InternalNode, LeafNode


logger = logging.getLogger(__name__)
logging.basicConfig(filename='parser.log', level=logging.INFO)


class TransitionBase(nn.Module):
    EMPTY_TOKEN = '-EMPTY-'  # used as dummy to encode an empty buffer or history
    EMPTY_INDEX = -1

    """A base class for the Stack, Buffer and History."""
    def __init__(self):
        super(TransitionBase, self).__init__()
        self._items = []

    def __str__(self):
        return f'{type(self).__name__}: {self.tokens}'

    def pop(self):
        assert len(self._items) > 0
        return self._items.pop()

    @property
    def items(self):
        return self._items

    @property
    def tokens(self):
        return [item.token for item in self.items]

    @property
    def indices(self):
        return [item.index for item in self.items]

    @property
    def embeddings(self):
        return [item.embedding for item in self.items]

    @property
    def encodings(self):
        return [item.encoding for item in self.items]

    @property
    def top(self):
        return self._items[-1]

    @property
    def top_item(self):
        return self._items[-1]

    @property
    def top_token(self):
        return self.top_item.token

    @property
    def top_index(self):
        return self.top_item.index

    @property
    def top_embedded(self):
        return self.top_item.embedding

    @property
    def top_encoded(self):
        return self.top_item.encoding


class Stack(TransitionBase):
    def __init__(self, word_embedding, nt_embedding, encoder, device):
        """Initialize the Stack.

        Arguments:
            word_embedding (nn.Embedding): embedding function for words.
            nt_embedding (nn.Embedding): embedding function for nonterminals.
            encoder (nn.Module): recurrent encoder.
            device: device on which computation is done (gpu or cpu).
        """
        super(Stack, self).__init__()
        self.word_embedding = word_embedding
        self.nt_embedding = nt_embedding
        self.encoder = encoder
        self.device = device
        self.num_open_nonterminals = 0
        self.empty_emb = nn.Parameter(torch.zeros(1, word_embedding.embedding_dim))

    def __str__(self):
        return f'{type(self).__name__} ({self.num_open_nonterminals} open NTs): {self.tokens}'

    def _reset(self):
        empty = Item(self.EMPTY_TOKEN, self.EMPTY_INDEX)
        empty.embedding = self.empty_emb
        empty.encoding = self.encoder(self.empty_emb)
        self._items = [InternalNode(empty)]
        self._start = True

    def initialize(self):
        self._reset()
        self.num_open_nonterminals = 0
        self.encoder.initialize_hidden()

    def open(self, nonterminal):
        assert isinstance(nonterminal, Nonterminal)
        nonterminal.embedding = self.nt_embedding(wrap([nonterminal.index], self.device))
        self.push(nonterminal)
        self.num_open_nonterminals += 1

    def push(self, item):
        assert isinstance(item, Item)
        item.encoding = self.encoder(item.embedding)  # give item new encoding
        if isinstance(item, Word):
            node = LeafNode(item)
        elif isinstance(item, Nonterminal):
            node = InternalNode(item)
        else:
            raise ValueError(f'ivalid {item} pushed onto stack')
        # Add child node to open nonterminal
        for head in self._items[::-1]:
            if head.is_open_nt:
                head.add_child(node)
                break
        self._items.append(node)

    def reduce(self):
        children = []
        while not self.top.is_open_nt:
            children.append(self.pop())
        children.reverse()
        sequence_len = len(children)
        head = self.top
        # Add nonterminal label to the beginning and end of children
        # TODO not completely correct...
        children = [child.item for child in children]  # List[Item]
        logger.debug('{:<23} {}'.format('head:', head))
        logger.debug('{:<23} {}'.format('reducing', [item.token for item in children]))
        children = [head.item] + children + [head.item]  # List[Item]
        # Package embeddings as pytorch tensor
        embeddings = [item.embedding.unsqueeze(0) for item in children]  # List[Variable]
        embeddings = torch.cat(embeddings, 1)  # tensor (batch, seq_len, emb_dim)
        reduced = self.encoder.composition(embeddings)
        logger.debug('{:<23} {}'.format('embeddings:', embeddings.data.shape))
        logger.debug('{:<23} {}'.format('reduced:', reduced.data))
        logger.debug('{:<23} {}'.format('head-embedding before:', self.top.item.embedding.data))
        self.top.item.embedding = reduced
        logger.debug('{:<23} {}'.format('head-embedding after:', self.top.item.embedding.data))
        logger.debug('{:<23} {}'.format('hidden before:', self.encoder.hx1.data))
        self.reset_hidden(sequence_len)
        logger.debug('{:<23} {}'.format('head-encoding before:', self.top.item.encoding.data))
        self.top.item.encoding = self.encoder(self.top.item.embedding)
        logger.debug('{:<23} {}'.format('hidden after:', self.encoder.hx1.data))
        logger.debug('{:<23} {}'.format('head-encoding after:', self.top.item.encoding.data))
        self.top.close()  # No longer an open nonterminal
        logger.debug('{:<23} {}'.format('top item is open:', self.top.is_open_nt))
        self.num_open_nonterminals -= 1

    def reset_hidden(self, sequence_len):
        # TODO change _reset_hidden is encoder so we can remove +1
        self.encoder._reset_hidden(sequence_len + 1)

    def get_tree(self):
        return self._items[1].linearize()

    def is_empty(self):
        if len(self._items) == 2:
            return not self.top.is_open_nt
        else:
            return False

    @property
    def empty(self):
        start = len(self._items) == 1
        return not start and not self.top_item.is_open_nt

    @property
    def items(self):
        return [node.item for node in self._items]

    @property
    def top_item(self):
        return self.items[-1]

    @property
    def top_embedded(self):
        return self.items[-1].item.embedding

    @property
    def top_encoded(self):
        return self.items[-1].item.encoding


class Buffer(TransitionBase):
    def __init__(self, embedding, encoder, device):
        """Initialize the Buffer.

        Arguments:
            embedding (nn.Embedding): embedding function for words on the buffer.
            encoder (nn.Module): encoder function to encode buffer contents.
            device: device on which computation is done (gpu or cpu).
        """
        super(Buffer, self).__init__()
        self.embedding = embedding
        self.encoder = encoder
        self.device = device
        self.empty_emb = nn.Parameter(torch.zeros(1, embedding.embedding_dim))

    def _reset(self):
        empty = Action(self.EMPTY_TOKEN, self.EMPTY_INDEX)
        empty.embedding = self.empty_emb
        self._items = [empty]

    def initialize(self, sentence):
        """Embed and encode the sentence."""
        self._reset()
        self._items += sentence[::-1]
        # Embed items without the first element, which is EMPTY and already embedded.
        embeddings = self.embedding(wrap(self.indices[1:], self.device))  # (seq_len, emb_dim)
        empty_embedding = self.items[0].embedding
        embeddings = torch.cat((empty_embedding, embeddings), dim=0)
        # Encode everything together
        encodings = self.encoder(embeddings.unsqueeze(0))  # (1, seq_len, hidden_size)
        for i, item in enumerate(self._items):
            item.embedding = embeddings[i, :].unsqueeze(0)  # (1, emb_dim)
            item.encoding = encodings[:, i, :]  # (1, hidden_size)

    @property
    def empty(self):
        return len(self._items) == 1


class Terminals(TransitionBase):
    pass


class History(TransitionBase):
    def __init__(self, word_embedding, nt_embedding, action_embedding, encoder, device):
        """Initialize the History.

        Arguments:
            embedding (nn.Embedding): embedding function for actions.
            device: device on which computation is done (gpu or cpu).
        """
        super(History, self).__init__()
        assert word_embedding.embedding_dim == action_embedding.embedding_dim
        self.word_embedding = word_embedding
        self.nt_embedding = nt_embedding
        self.action_embedding = action_embedding
        self.encoder = encoder
        self.device = device
        self.empty_emb = nn.Parameter(torch.zeros(1, word_embedding.embedding_dim))

    def _reset(self):
        empty = Action(self.EMPTY_TOKEN, self.EMPTY_INDEX)
        empty.embedding = self.empty_emb
        empty.encoding = self.encoder(self.empty_emb)
        self._items = [empty]

    def initialize(self):
        """Initialize the history by pushing the `empty` item."""
        self._reset()
        self.encoder.initialize_hidden()

    def push(self, action):
        assert isinstance(action, Action)
        # Embed the action.
        if action.is_nt:
            nt = action.get_nt()
            action.embedding = self.nt_embedding(wrap([nt.index], self.device))
        elif action.is_gen:
            word = action.get_gen()
            action.embedding = self.word_embedding(wrap([word.index], self.device))
        else:  # Shift or Reduce
            action.embedding = self.action_embedding(wrap([action.index], self.device))
        # Encode the action.
        action.encoding = self.encoder(action.embedding)
        self._items.append(action)

    @property
    def actions(self):
        return [token for token in self.items[1:]]  # First item in self._items is the empty item

    @property
    def empty(self):
        return len(self._items) == 1


class Parser(nn.Module):
    """The parse configuration."""
    def __init__(self, word_embedding, nt_embedding, action_embedding,
                 stack_encoder, buffer_encoder, history_encoder, device=None):
        """Initialize the parser.

        Arguments:
            word_embedding: embedding function for words.
            nt_embedding: embedding function for nonterminals.
            actions_embedding: embedding function for actions.
            buffer_encoder: encoder function to encode buffer contents.
            actions (tuple): tuple with indices of actions.
            device: device on which computation is done (gpu or cpu).
        """
        super(Parser, self).__init__()
        self.stack = Stack(word_embedding, nt_embedding, stack_encoder, device)
        self.buffer = Buffer(word_embedding, buffer_encoder, device)
        self.history = History(word_embedding, nt_embedding, action_embedding, history_encoder, device)
        # TODO: self.terminals = Terminals(...)

    def __str__(self):
        return '\n'.join(('Parser', str(self.stack), str(self.buffer), str(self.history)))

    def initialize(self, sentence: List[Word]):
        """Initialize all the components of the parser."""
        self.buffer.initialize(sentence)  # items: List[Word]
        self.stack.initialize()
        self.history.initialize()

    def _can_shift(self):
        cond1 = not self.buffer.empty
        cond2 = self.stack.num_open_nonterminals > 0
        return cond1 and cond2

    def _can_gen(self):
        # TODO
        return True

    def _can_open(self):
        cond1 = not self.buffer.empty
        cond2 = self.stack.num_open_nonterminals < 100
        return cond1 and cond2

    def _can_reduce(self):
        cond1 = not self.last_action.is_nt
        cond3 = self.stack.num_open_nonterminals > 1
        cond4 = self.buffer.empty
        return (cond1 and cond3) or cond4

    def _shift(self):
        assert self._can_shift()
        self.stack.push(self.buffer.pop())

    def _gen(self, word):
        assert isinstance(word, Word)
        assert self._can_gen()
        self.terminals.push(word)

    def _open(self, nonterminal):
        assert isinstance(nonterminal, Nonterminal)
        assert self._can_open()
        self.stack.open(nonterminal)

    def _reduce(self):
        assert self._can_reduce()
        self.stack.reduce()

    def get_encoded_input(self):
        """Return the representations of the stack, buffer and history."""
        # TODO AttributeError: 'Stack' object has no attribute 'top_encoded'.
        # stack = self.stack.top_encoded      # (batch, word_lstm_hidden)
        stack = self.stack.top_item.encoding      # (batch, word_lstm_hidden)
        buffer = self.buffer.top_encoded    # (batch, word_lstm_hidden)
        history = self.history.top_encoded  # (batch, action_lstm_hidden)
        return stack, buffer, history

    def parse_step(self, action):
        """Updates parser one step give the action."""
        assert isinstance(action, Action)
        if action == SHIFT:
            self._shift()
        elif action == REDUCE:
            self._reduce()
        elif action.is_gen:
            self._gen(action.get_gen())
        elif action.is_nt:
            self._open(action.get_nt())
        self.history.push(action)

    def is_valid_action(self, action):
        """Check whether the action is valid under the parser's configuration."""
        if action == SHIFT:
            return self._can_shift()
        elif action == REDUCE:
            return self._can_reduce()
        elif action.is_gen:
            return self._can_gen()
        elif action.is_nt:
            return self._can_open()
        else:
            raise ValueError(f'got illegal action: {action.token}.')

    @property
    def actions(self):
        """Return the current history of actions."""
        return self.history.actions

    @property
    def last_action(self):
        """Return the last action taken."""
        return self.history.top


if __name__ == '__main__':
    from data import SPECIAL_TOKENS
    from encoder import BiRecurrentEncoder, StackLSTM, BufferLSTM, HistoryLSTM
    from nn import MLP
    from loss import LossCompute


    tagged_tree = '(S (NP (NNP Avco) (NNP Corp.)) (VP (VBD received) (NP (NP (DT an) (ADJP (QP ($ $) (CD 11.8) (CD million))) (NNP Army) (NN contract)) (PP (IN for) (NP (NN helicopter) (NNS engines))))) (. .))'
    tree = '(S (NP Avco Corp.) (VP received (NP (NP an (ADJP (QP $ 11.8 million)) Army contract) (PP for (NP helicopter engines)))) .)'
    sentence = 'Avco Corp. received an $ 11.8 million Army contract for helicopter engines .'.split()
    actions = [
        'NT(S)',
        'NT(NP)',
        'SHIFT',
        'SHIFT',
        'REDUCE',
        'NT(VP)',
        'SHIFT',
        'NT(NP)',
        'NT(NP)',
        'SHIFT',
        'NT(ADJP)',
        'NT(QP)',
        'SHIFT',
        'SHIFT',
        'SHIFT',
        'REDUCE',
        'REDUCE',
        'SHIFT',
        'SHIFT',
        'REDUCE',
        'NT(PP)',
        'SHIFT',
        'NT(NP)',
        'SHIFT',
        'SHIFT',
        'REDUCE',
        'REDUCE',
        'REDUCE',
        'REDUCE',
        'SHIFT',
        'REDUCE'
    ]

    # A test sentence.
    # tagged_tree = "(S (NP (NN Champagne) (CC and) (NN dessert)) (VP (VBD followed)) (. .))"
    # tree = "(S (NP Champagne and dessert) (VP followed) .)"
    # sentence = "Champagne and dessert followed .".split()
    # actions = [
    #     'NT(S)',
    #     'NT(NP)',
    #     'SHIFT',
    #     'SHIFT',
    #     'SHIFT',
    #     'REDUCE',
    #     'NT(VP)',
    #     'SHIFT',
    #     'REDUCE',
    #     'SHIFT',
    #     'REDUCE'
    # ]


    def prepare_data(actions, sentence):
        i2n = list(SPECIAL_TOKENS) + [a[3:-1] for a in actions if a.startswith('NT')]
        i2w = list(SPECIAL_TOKENS) + [w for w in list(set(sentence))]
        n2i = dict((n, i) for i, n in enumerate(i2n))
        w2i = dict((w, i) for i, w in enumerate(i2w))
        action_items = []
        sentence_items = []
        for token in sentence:
            index = w2i[token]
            sentence_items.append(Word(token, index))
        for token in actions:
            if token == SHIFT.token:
                action = SHIFT
            elif token == REDUCE.token:
                action = REDUCE
            elif token.startswith('NT'):
                nt = token[3:-1]
                nt = Nonterminal(nt, n2i[nt])
                action = NT(nt)
            elif token.startswith('GEN'):
                word = token[4:-1]
                word = Word(word, w2i[word])
                action = GEN(word)
            action_items.append(action)
        return action_items, sentence_items, len(i2w), len(i2n)


    def test_parser(actions, sentence, dim=4):
        assert dim % 2 == 0
        actions, sentence, num_words, num_nonterm = prepare_data(actions, sentence)
        word_embedding = nn.Embedding(num_words, dim)
        nt_embedding = nn.Embedding(num_nonterm, dim)
        action_embedding = nn.Embedding(3, dim)

        stack_encoder = StackLSTM(dim, dim, dropout=0.3)
        buffer_encoder = BufferLSTM(dim, dim, 2, dropout=0.3)
        history_encoder = HistoryLSTM(dim, dim, dropout=0.3)
        reducer = BiRecurrentEncoder(dim, dim//2, 2, dropout=0.3)
        parser = Parser(
            word_embedding,
            nt_embedding,
            action_embedding,
            stack_encoder,
            buffer_encoder,
            history_encoder,
        )
        parser.initialize(sentence)
        for i, action in enumerate(actions):
            logger.debug('--------')
            logger.debug(f'Step {i:>3}')
            logger.debug(parser.stack)
            logger.debug(parser.buffer)
            logger.debug(parser.history)
            logger.debug('action: {}'.format(action.token))
            parser.parse_step(action)
            if i > 0:
                logger.debug('partial tree: {}'.format(parser.stack.get_tree()))
                logger.debug('')
        logger.debug('--------')
        logger.debug('Finished')
        logger.debug(parser.stack)
        logger.debug(parser.buffer)
        logger.debug(parser.history)
        logger.debug(f'open nonterminals: {parser.stack.num_open_nonterminals}')
        logger.debug('')
        logger.debug('pred: {}'.format(parser.stack.get_tree()))
        logger.debug('gold: {}'.format(tree))


    def forward(model, actions, sentence):
        parser, reducer, action_mlp, nonterminal_mlp = model
        parser.initialize(sentence)
        loss_compute = LossCompute(nn.CrossEntropyLoss, device=None)
        loss = torch.zeros(1)
        for i, action in enumerate(actions):
            # Compute loss
            stack, buffer, history = parser.get_encoded_input()
            x = torch.cat((buffer, history, stack), dim=-1)
            action_logits = action_mlp(x)
            loss += loss_compute(action_logits, action.action_index)
            # If we open a nonterminal, predict which.
            if action.is_nt:
                nonterminal_logits = nonterminal_mlp(x)
                nt = action.get_nt()
                loss += loss_compute(nonterminal_logits, nt.index)
            parser.parse_step(action)
        return loss


    def test_train(actions, sentence, steps, dim=4):
        assert dim % 2 == 0
        num_actions = 3
        data = prepare_data(actions, sentence)
        actions, sentence, num_words, num_nonterm = data

        word_embedding = nn.Embedding(num_words, dim)
        nt_embedding = nn.Embedding(num_nonterm, dim)
        action_embedding = nn.Embedding(num_actions, dim)

        stack_encoder = StackLSTM(dim, dim, dropout=0.3)
        buffer_encoder = BufferLSTM(dim, dim, 2, dropout=0.3)
        history_encoder = HistoryLSTM(dim, dim, dropout=0.3)
        parser = Parser(
            word_embedding,
            nt_embedding,
            action_embedding,
            stack_encoder,
            buffer_encoder,
            history_encoder,
        )
        reducer = BiRecurrentEncoder(dim, dim//2, 2, dropout=0.3)
        action_mlp = MLP(3*dim, dim, num_actions)
        nonterminal_mlp = MLP(3*dim, dim, num_nonterm)

        parameters = (
            list(parser.parameters()) +
            list(reducer.parameters()) +
            list(action_mlp.parameters()) +
            list(nonterminal_mlp.parameters())
        )
        optimizer = torch.optim.Adam(parameters, lr=0.001)
        model = (parser, reducer, action_mlp, nonterminal_mlp)
        for i in range(steps):
            parser.initialize(sentence)
            loss = forward(model, actions, sentence)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            print('loss', loss.item(), end='\r')


    # test_parser(actions, sentence)
    test_train(actions, sentence, dim=50, steps=10000)