"""Adapted from Nematode: https://github.com/demelin/nematode """

import sys
import tensorflow as tf
import numpy
from docutils.nodes import target

from sparse_sgcn import gcn, GCN
from sgcn import gcn as gcn_dense

# ModuleNotFoundError is new in 3.6; older versions will throw SystemError
if sys.version_info < (3, 6):
    ModuleNotFoundError = SystemError

try:
    from . import model_inputs
    from . import mrt_utils as mru
    from .sampling_utils import SamplingUtils
    from . import tf_utils
    from .transformer_blocks import AttentionBlock, FFNBlock
    from .transformer_layers import \
        EmbeddingLayer, \
        MaskedCrossEntropy, \
        get_right_context_mask, \
        get_positional_signal, \
        get_tensor_from_times, \
        get_all_times
    from .util import load_dict
    from .tensorflow.python.ops.ragged.ragged_util import repeat
except (ModuleNotFoundError, ImportError) as e:
    import model_inputs
    import mrt_utils as mru
    from sampling_utils import SamplingUtils
    import tf_utils
    from transformer_blocks import AttentionBlock, FFNBlock
    from transformer_layers import \
        EmbeddingLayer, \
        MaskedCrossEntropy, \
        get_right_context_mask, \
        get_positional_signal, \
        get_tensor_from_times, \
        get_all_times
    from util import load_dict
    from tensorflow.python.ops.ragged.ragged_util import repeat

INT_DTYPE = tf.int32
FLOAT_DTYPE = tf.float32

class Transformer(object):
    """ The main transformer model class. """

    def __init__(self, config):
        # Set attributes
        self.config = config
        self.source_vocab_size = config.source_vocab_sizes[0]
        self.target_vocab_size = config.target_vocab_size
        self.target_labels_num = config.target_labels_num
        self.name = 'transformer'
        # load dictionary token-> token_id
        model_type = self.name
        self.target_labels_dict = load_dict(config.target_dict, model_type) if config.target_graph else {}

        # Placeholders
        self.inputs = model_inputs.ModelInputs(config)

        # Convert from time-major to batch-major, handle factors
        self.source_ids, \
            self.source_mask, \
            self.target_ids_in, \
            self.target_ids_out, \
            self.target_mask, \
            self.edge_times, \
            self.label_times = self._convert_inputs(self.inputs)
        # self.source_ids, \
        #     self.source_mask, \
        #     self.target_ids_in, \
        #     self.target_ids_out, \
        #     self.target_mask, \
        #     self.edge_labels, \
        #     self.bias_labels, \
        #     self.general_edge_mask, \
        #     self.general_bias_mask = self._convert_inputs(self.inputs)

        self.training = self.inputs.training
        self.scores = self.inputs.scores
        self.index = self.inputs.index

        # Build the common parts of the graph.
        with tf.compat.v1.name_scope('{:s}_loss'.format(self.name)):
            # (Re-)generate the computational graph
            self.dec_vocab_size = self._build_graph()

        # Build the training-specific parts of the graph.

        with tf.compat.v1.name_scope('{:s}_loss'.format(self.name)):
            # Encode source sequences
            with tf.compat.v1.name_scope('{:s}_encode'.format(self.name)):
                enc_output, cross_attn_mask = self.enc.encode(
                    self.source_ids, self.source_mask)
            # Decode into target sequences
            with tf.compat.v1.name_scope('{:s}_decode'.format(self.name)):
                logits = self.dec.decode_at_train(self.target_ids_in,
                                                  enc_output,
                                                  cross_attn_mask, self.edge_times, self.label_times)
                # logits = self.dec.decode_at_train(self.target_ids_in,
                #                                   enc_output,
                #                                   cross_attn_mask, self.edge_labels, self.bias_labels, self.general_edge_mask, self.general_bias_mask)
            # logits = tf.Print(logits, [tf.shape(self.target_ids_in)], "target_ids_in", 3)
            # logits = tf.Print(logits, [tf.shape(self.target_ids_out)], "target_ids_out", 3)
            # Instantiate loss layer(s)
            print_ops = []
            print_ops.append(tf.Print([], [tf.shape(logits), logits[0,:,0]], "logits shapes", 50, 100))
            print_ops.append(tf.Print([], [tf.shape(self.target_ids_out), self.target_ids_out], "target_ids_out", 50, 100))
            print_ops.append(tf.Print([], [tf.shape(self.target_ids_in), self.target_ids_in], "target_ids_in", 50, 100))
            with tf.control_dependencies(print_ops):
                logits = logits * 1 #TODO delete
                loss_layer = MaskedCrossEntropy(self.dec_vocab_size,
                                                self.config.label_smoothing,
                                                INT_DTYPE,
                                                FLOAT_DTYPE,
                                                time_major=False,
                                                name='loss_layer')
            # Calculate loss
            masked_loss, sentence_loss, batch_loss = \
                loss_layer.forward(logits, self.target_ids_out, self.target_mask, self.training)
            if self.config.print_per_token_pro:
                # e**(-(-log(probability))) =  probability
                self._print_pro = tf.math.exp(-masked_loss)

            sent_lens = tf.reduce_sum(input_tensor=self.target_mask, axis=1, keepdims=False)
            self._loss_per_sentence = sentence_loss * sent_lens
            self._loss = tf.reduce_mean(input_tensor=self._loss_per_sentence, keepdims=False)

            # calculate expected risk
            if self.config.loss_function == 'MRT':
                # self._loss_per_sentence is negative log probability of the output sentence, each element represents
                # the loss of each sample pair.
                self._risk = mru.mrt_cost(self._loss_per_sentence, self.scores, self.index, self.config)

            self.sampling_utils = SamplingUtils(config)


    def _build_graph(self):
        """ Defines the model graph. """
        with tf.compat.v1.variable_scope('{:s}_model'.format(self.name)):
            # Instantiate embedding layer(s)
            if not self.config.tie_encoder_decoder_embeddings:
                enc_vocab_size = self.source_vocab_size
                dec_vocab_size = self.target_vocab_size
            else:
                assert self.source_vocab_size == self.target_vocab_size, \
                    'Input and output vocabularies should be identical when tying embedding tables.'
                enc_vocab_size = dec_vocab_size = self.source_vocab_size

            encoder_embedding_layer = EmbeddingLayer(enc_vocab_size,
                                                     self.config.embedding_size,
                                                     self.config.state_size,
                                                     FLOAT_DTYPE,
                                                     name='encoder_embedding_layer')
            if not self.config.tie_encoder_decoder_embeddings:
                decoder_embedding_layer = EmbeddingLayer(dec_vocab_size,
                                                         self.config.embedding_size,
                                                         self.config.state_size,
                                                         FLOAT_DTYPE,
                                                         name='decoder_embedding_layer')
            else:
                decoder_embedding_layer = encoder_embedding_layer

            if not self.config.tie_encoder_decoder_embeddings:
                softmax_projection_layer = EmbeddingLayer(dec_vocab_size,
                                                          self.config.embedding_size,
                                                          self.config.state_size,
                                                          FLOAT_DTYPE,
                                                          name='softmax_projection_layer')
            else:
                softmax_projection_layer = decoder_embedding_layer

            # Instantiate the component networks
            self.enc = TransformerEncoder(self.config,
                                          encoder_embedding_layer,
                                          self.training,
                                          'encoder')
            self.dec = TransformerDecoder(self.config,
                                          decoder_embedding_layer,
                                          softmax_projection_layer,
                                          self.training,
                                          # self.int_dtype,
                                          # self.float_dtype,
                                          'decoder',
                                          labels_num=self.target_labels_num,
                                          labels_dict=self.target_labels_dict
                                          )
        return dec_vocab_size

    @property
    def loss_per_sentence(self):
        return self._loss_per_sentence

    @property
    def loss(self):
        return self._loss

    @property
    def risk(self):
        return self._risk

    @property
    def print_pro(self):
        return self._print_pro

    def _convert_inputs(self, inputs):
        # Convert from time-major to batch-major. Note that we take factor 0
        # from x and ignore any other factors.
        source_ids = tf.transpose(a=inputs.x[0], perm=[1, 0])
        source_mask = tf.transpose(a=inputs.x_mask, perm=[1, 0])
        target_ids_out = tf.transpose(a=inputs.y, perm=[1, 0])
        target_mask = tf.transpose(a=inputs.y_mask, perm=[1, 0])

        if self.config.target_graph:
            edge_times = inputs.edge_times
            label_times = inputs.label_times
            edge_times = tf.sparse.transpose(edge_times, perm=[len(edge_times.shape) - 1] + list(range(len(edge_times.shape) - 1)))
            label_times = tf.sparse.transpose(label_times, perm=[len(label_times.shape) - 1] + list(range(len(label_times.shape) - 1)))
        else:
            edge_times = None
            label_times = None

        # target_ids_in is a bit more complicated since we need to insert
        # the special <GO> symbol (with value 1) at the start of each sentence
        max_len, batch_size = tf.shape(input=inputs.y)[0], tf.shape(input=inputs.y)[1]
        go_symbols = tf.fill(value=1, dims=[1, batch_size])
        tmp = tf.concat([go_symbols, inputs.y], 0)
        tmp = tmp[:-1, :]
        target_ids_in = tf.transpose(a=tmp, perm=[1,0])
        return (source_ids, source_mask, target_ids_in, target_ids_out,
                target_mask, edge_times, label_times)
        # return (source_ids, source_mask, target_ids_in, target_ids_out,
        #         target_mask, edge_labels, bias_labels, general_edge_mask, general_bias_mask)


class TransformerEncoder(object):
    """ The encoder module used within the transformer model. """

    def __init__(self,
                 config,
                 embedding_layer,
                 training,
                 name):
        # Set attributes
        self.config = config
        self.embedding_layer = embedding_layer
        self.training = training
        self.name = name

        # Track layers
        self.encoder_stack = dict()
        self.is_final_layer = False

        # Create nodes
        self._build_graph()

    def _embed(self, index_sequence):
        """ Embeds source-side indices to obtain the corresponding dense tensor representations. """
        # Embed input tokens
        return self.embedding_layer.embed(index_sequence)

    def _build_graph(self):
        """ Defines the model graph. """
        # Initialize layers
        with tf.compat.v1.variable_scope(self.name):
            for layer_id in range(1, self.config.transformer_enc_depth + 1):
                layer_name = 'layer_{:d}'.format(layer_id)
                # Check if constructed layer is final
                if layer_id == self.config.transformer_enc_depth:
                    self.is_final_layer = True
                # Specify ffn dimensions sequence
                ffn_dims = [self.config.transformer_ffn_hidden_size, self.config.state_size]
                with tf.compat.v1.variable_scope(layer_name):
                    # Build layer blocks (see layers.py)
                    self_attn_block = AttentionBlock(self.config,
                                                     FLOAT_DTYPE,
                                                     self_attention=True,
                                                     training=self.training)
                    ffn_block = FFNBlock(self.config,
                                         ffn_dims,
                                         FLOAT_DTYPE,
                                         is_final=self.is_final_layer,
                                         training=self.training)

                # Maintain layer-wise dict entries for easier data-passing (may
                # change later)
                self.encoder_stack[layer_id] = dict()
                self.encoder_stack[layer_id]['self_attn'] = self_attn_block
                self.encoder_stack[layer_id]['ffn'] = ffn_block

    def encode(self, source_ids, source_mask):
        """ Encodes source-side input tokens into meaningful, contextually-enriched representations. """

        def _prepare_source():
            """ Pre-processes inputs to the encoder and generates the corresponding attention masks."""
            # Embed
            source_embeddings = self._embed(source_ids)
            # Obtain length and depth of the input tensors
            _, time_steps, depth = tf_utils.get_shape_list(source_embeddings)
            # Transform input mask into attention mask
            inverse_mask = tf.cast(tf.equal(source_mask, 0.0), dtype=FLOAT_DTYPE)
            attn_mask = inverse_mask * -1e9
            # Expansion to shape [batch_size, 1, 1, time_steps] is needed for
            # compatibility with attention logits
            attn_mask = tf.expand_dims(tf.expand_dims(attn_mask, 1), 1)
            # Differentiate between self-attention and cross-attention masks
            # for further, optional modifications
            self_attn_mask = attn_mask
            cross_attn_mask = attn_mask
            # Add positional encodings
            positional_signal = get_positional_signal(time_steps, depth, FLOAT_DTYPE)
            source_embeddings += positional_signal
            # Apply dropout
            if self.config.transformer_dropout_embeddings > 0:
                source_embeddings = tf.compat.v1.layers.dropout(source_embeddings,
                                                      rate=self.config.transformer_dropout_embeddings, training=self.training)
            return source_embeddings, self_attn_mask, cross_attn_mask

        with tf.compat.v1.variable_scope(self.name):
            # Prepare inputs to the encoder, get attention masks
            enc_inputs, self_attn_mask, cross_attn_mask = _prepare_source()
            # Propagate inputs through the encoder stack
            enc_output = enc_inputs
            for layer_id in range(1, self.config.transformer_enc_depth + 1):
                enc_output, _ = self.encoder_stack[layer_id][
                    'self_attn'].forward(enc_output, None, self_attn_mask)
                enc_output = self.encoder_stack[
                    layer_id]['ffn'].forward(enc_output)
        return enc_output, cross_attn_mask

class TransformerDecoder(object):
    """ The decoder module used within the transformer model. """

    def __init__(self,
                 config,
                 embedding_layer,
                 softmax_projection_layer,
                 training,
                 name,
                 from_rnn=False, transition_idx={}, labels_dict={}, labels_num=None):

        # Set attributes
        self.config = config
        self.embedding_layer = embedding_layer
        self.softmax_projection_layer = softmax_projection_layer
        self.training = training
        self.name = name
        self.from_rnn = from_rnn
        self.labels_num = labels_num
        self.labels_dict = labels_dict

        # If the decoder is used in a hybrid system, adjust parameters
        # accordingly
        self.time_dim = 0 if from_rnn else 1

        # Track layers
        self.decoder_stack = dict()
        self.gcn_stack = dict()
        self.is_final_layer = False

        # Create nodes
        self._build_graph()

    def extract_target_graph(self, target_ids):
        return target_ids

    def _embed(self, index_sequence):
        """ Embeds target-side indices to obtain the corresponding dense tensor representations. """
        return self.embedding_layer.embed(index_sequence)

    def _get_initial_memories(self, batch_size, beam_size):
        """ Initializes decoder memories used for accelerated inference. """
        initial_memories = dict()
        for layer_id in range(1, self.config.transformer_dec_depth + 1):
            initial_memories['layer_{:d}'.format(layer_id)] = \
                {'keys': tf.tile(tf.zeros([batch_size, 0, self.config.state_size]), [beam_size, 1, 1]),
                 'values': tf.tile(tf.zeros([batch_size, 0, self.config.state_size]), [beam_size, 1, 1])}
        return initial_memories

    def _build_graph(self):
        """ Defines the model graph. """
        # Initialize gcn layers
        if self.config.target_graph:
            for layer_id in range(self.config.target_gcn_layers):
                self.gcn_stack[layer_id] = GCN(self.embedding_layer.hidden_size, vertices_num=self.config.maxlen + 1, bias_labels_num=self.labels_num, edge_labels_num=3,
                    activation=tf.nn.relu, use_bias=True, gate=True) #TODO use bias, use gate
        # Initialize layers
        with tf.compat.v1.variable_scope(self.name):
            for layer_id in range(1, self.config.transformer_dec_depth + 1):
                layer_name = 'layer_{:d}'.format(layer_id)
                # Check if constructed layer is final
                if layer_id == self.config.transformer_dec_depth:
                    self.is_final_layer = True
                # Specify ffn dimensions sequence
                ffn_dims = [self.config.transformer_ffn_hidden_size, self.config.state_size]
                with tf.compat.v1.variable_scope(layer_name):
                    # Build layer blocks (see layers.py)
                    self_attn_block = AttentionBlock(self.config,
                                                     FLOAT_DTYPE,
                                                     self_attention=True,
                                                     training=self.training)
                    cross_attn_block = AttentionBlock(self.config,
                                                      FLOAT_DTYPE,
                                                      self_attention=False,
                                                      training=self.training,
                                                      from_rnn=self.from_rnn)
                    ffn_block = FFNBlock(self.config,
                                         ffn_dims,
                                         FLOAT_DTYPE,
                                         is_final=self.is_final_layer,
                                         training=self.training)

                # Maintain layer-wise dict entries for easier data-passing (may
                # change later)
                self.decoder_stack[layer_id] = dict()
                self.decoder_stack[layer_id]['self_attn'] = self_attn_block
                self.decoder_stack[layer_id]['cross_attn'] = cross_attn_block
                self.decoder_stack[layer_id]['ffn'] = ffn_block

    def decode_at_train(self, target_ids, enc_output, cross_attn_mask, edge_times, labels_times):
        """ Returns the probability distribution over target-side tokens conditioned on the output of the encoder;
         performs decoding in parallel at training time. """
        def _decode_all(target_embeddings):
            """ Decodes the encoder-generated representations into target-side logits in parallel. """
            #TODO embedd per token and perhaps parallel the second to last decoder blocks
            dec_input = target_embeddings
            # add gcn layers
            if self.config.target_graph:
                for layer_id in range(self.config.target_gcn_layers):
                    orig_input = dec_input
                    inputs = [dec_input, edges, labels]
                    print_ops = []
                    print_ops.append(tf.Print([], [tf.shape(item) for item in inputs], "input shapes", 50, 100))
                    print_ops.append(tf.Print([], [tf.shape(enc_output), enc_output], "end_out shape", 50, 100))
                    with tf.control_dependencies(print_ops):
                        dec_input = self.gcn_stack[layer_id].apply(inputs)
                    dec_input += orig_input # residual connection
                dec_input = dec_input[:, :timesteps, :] # slice tensor to save space

            # Propagate inputs through the encoder stack
            dec_output = dec_input
            for layer_id in range(1, self.config.transformer_dec_depth + 1):
                print_ops = []
                print_ops.append(tf.Print([], [tf.shape(dec_output), dec_output[0,:,0]], "input to self attn first emb dim", 50, 1000))
                print_ops.append(tf.Print([], [tf.shape(dec_output), dec_output[0,:,-1]], "input to self attn last emb dim", 50, 1000))
                print_ops.append(
                    tf.Print([], [tf.shape(dec_output), dec_output[-1, :, 0]], "last in batch - input to self attn first emb dim", 50,
                             1000))
                print_ops.append(
                    tf.Print([], [tf.shape(dec_output), dec_output[-1, :, -1]], "last in batch - input to self attn last emb dim", 50,
                             1000))
                # print_ops.append(tf.Print([], [tf.shape(self_attn_mask), self_attn_mask], "self_attn_mask"))
                with tf.control_dependencies(print_ops):
                    dec_output, _ = self.decoder_stack[layer_id][
                        'self_attn'].forward(dec_output, None, self_attn_mask) # avoid attending sentences with no words and words after the sentence (zeros)
                print_ops = []
                print_ops.append(tf.Print([], [tf.shape(dec_input), dec_input], "dec_input shape", 50, 100))
                print_ops.append(tf.Print([], [tf.shape(dec_output)], "after block" + str(layer_id), 50, 100))
                print_ops.append(tf.Print([], [tf.shape(cross_attn_mask), cross_attn_mask], "cross attention" + str(layer_id), 50, 100))
                with tf.control_dependencies(print_ops):
                    dec_output, _ = \
                        self.decoder_stack[layer_id]['cross_attn'].forward(
                        dec_output, enc_output, cross_attn_mask) #TODO what happens with cross attention (currently untiled)
                print_ops = []
                print_ops.append(tf.Print([], [tf.shape(dec_output)], "decoded succsessfully", 50, 100))
                with tf.control_dependencies(print_ops):
                    dec_output = self.decoder_stack[
                        layer_id]['ffn'].forward(dec_output)
            return dec_output

        def _prepare_targets():
            """ Pre-processes target token ids before they're passed on as input to the decoder
            for parallel decoding. """

            if self.config.target_graph:
                #padding == self.config.maxlen - tf.shape(target_ids)[1] == self.config.maxlen - tf.shape(positional_signal)
                padding = self.config.maxlen + 1 - timesteps
                printops = []
                printops.append(tf.Print([], [tf.shape(target_ids), target_ids[:4,:40]], "target_ids shape and two first in batch", 50, 300))
                printops.append(tf.Print([], [target_shape, tf.shape(target_ids)[1]], "target shape", 300, 50))
                printops.append(tf.Print([], [self.config.maxlen], "maxlen", 300, 50))
                with tf.control_dependencies(printops):
                    padded_target_ids = tf.pad(target_ids, [[0, 0], [0, padding]])
                    padded_positional_signal = tf.pad(positional_signal, [[0, 0], [0, padding], [0, 0]])
            else:
                padded_target_ids = target_ids
                padded_positional_signal = positional_signal
            # Embed target_ids
            target_embeddings = self._embed(padded_target_ids)
            target_embeddings += padded_positional_signal

            if self.config.transformer_dropout_embeddings > 0:
                target_embeddings = tf.compat.v1.layers.dropout(target_embeddings,
                                                      rate=self.config.transformer_dropout_embeddings, training=self.training)
            return target_embeddings

        def _decoding_function():
            """ Generates logits for target-side tokens. """
            # Embed the model's predictions up to the current time-step; add
            # positional information, mask
            target_embeddings = _prepare_targets()

            # Pass encoder context and decoder embeddings through the decoder
            dec_output = _decode_all(target_embeddings)
            # Project decoder stack outputs and apply the soft-max
            # non-linearity
            printops = []
            printops.append(
                tf.Print([], [tf.shape(dec_output), dec_output], "dec_output", 300, 50))
            with tf.control_dependencies(printops):
                full_logits = self.softmax_projection_layer.project(dec_output)
            return full_logits

        with tf.compat.v1.variable_scope(self.name):
            # Transpose encoder information in hybrid models
            if self.from_rnn:
                enc_output = tf.transpose(a=enc_output, perm=[1, 0, 2])
                cross_attn_mask = tf.transpose(a=cross_attn_mask, perm=[3, 1, 2, 0])

            target_shape = tf.shape(target_ids)
            batch_size = target_shape[0]
            timesteps = target_shape[-1]
            printops = []
            printops.append(
                tf.Print([], [timesteps], "timestep changes?", 300, 50))
            printops.append(tf.Print([], [target_shape], "target shape", 300, 50))
            printops.append(tf.Print([], [self.config.maxlen], "maxlen", 300, 50))
            printops.append(tf.Print([], [tf.shape(input=target_ids), target_ids], "target_ids are they like decoded x (if not should decoded x lose the beginning 1=<GO>?)", 300, 50))
            with tf.control_dependencies(printops):
                self_attn_mask = get_right_context_mask(timesteps)
            positional_signal = get_positional_signal(timesteps,
                                                      self.config.embedding_size,
                                                      FLOAT_DTYPE)
            if self.config.target_graph:
                # self_attn_mask = None
                # self_attn_mask = repeat(self_attn_mask, batch_size, 2)
                self_attn_mask = tf.tile(self_attn_mask, [1, 1, batch_size, 1])
                self_attn_mask = tf.transpose(self_attn_mask, [2, 1, 0, 3])
                # self_attn_mask = tf.reshape(self_attn_mask, [batch_size  * timesteps, -1, timesteps, 1])
                #TODO self_attn_mask is wrong is it currently duplicating (None looks on current empty words too)
                target_ids = repeat(target_ids, timesteps, 0)
                diagonals_mask = tf.ones([timesteps, timesteps], dtype=target_ids.dtype)
                diagonals_mask = tf.matrix_band_part(diagonals_mask, -1, 0)
                # diagonals_mask = tf.linalg.set_diag(diagonals_mask, tf.zeros(tf.shape(diagonals_mask)[0:-1], dtype = target_ids.dtype))
                diagonals_mask = tf.tile(diagonals_mask, [batch_size, 1])
                target_ids *= diagonals_mask
                edges = get_all_times(timesteps, edge_times)
                labels = get_all_times(timesteps, labels_times)
                # edges = get_tensor_from_times(timestep, labels_times)
                # labels = get_tensor_from_times(timestep, labels_times)

                # edges = tf.cast(edge_times, dtype=tf.float32)
                # labels = tf.cast(labels_times, dtype=tf.float32)
                # printops = []
                # # printops.append(
                # #     tf.Print([], [tf.shape(try_all_times), try_all_times.indices, try_all_times.values], "get_all_times_check", 300, 50))
                # printops.append(
                #     tf.Print([], [tf.shape(self_attn_mask), self_attn_mask[:,:,:,:20]], "masked attention", 300, 50))
                # printops.append(
                #     tf.Print([], [tf.shape(edges), edges.indices, edges.values], "masked edges", 300, 50))
                # printops.append(
                #     tf.Print([], [tf.shape(labels), labels.indices, labels.values], "masked labels", 300, 50))
                # with tf.control_dependencies(printops):
                logits = _decoding_function()
                diag = tf.range(timesteps)
                diag = tf.expand_dims(diag, 1)
                diag = tf.concat([diag, diag], 1) # [[1,1],[2,2]...[timesteps,timesteps]]
                diag = repeat(diag, self.config.target_vocab_size, 0)

                vocab_locs = tf.range(self.config.target_vocab_size)
                vocab_locs = tf.tile(vocab_locs, [timesteps])
                vocab_locs = tf.expand_dims(vocab_locs, 1)
                indices = tf.concat([diag, vocab_locs], 1)
                indices = tf.tile(indices, [batch_size, 1])


                logits = tf.gather_nd(logits, indices)
                logits = tf.reshape(logits, [batch_size, timesteps, self.config.target_vocab_size])
            else:
                logits = _decoding_function()
        return logits
