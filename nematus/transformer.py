"""Adapted from Nematode: https://github.com/demelin/nematode """

import sys
import tensorflow as tf

from sparse_sgcn import gcn, GCN

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
    get_all_times, \
    EdgeConstrain
    from .util import load_dict, parse_transitions
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
        get_all_times, \
        EdgeConstrain
    from util import load_dict, parse_transitions
    from tensorflow.python.ops.ragged.ragged_util import repeat

INT_DTYPE = tf.int32
FLOAT_DTYPE = tf.float32

print("Delete squash")
SQUASH = not True

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
        self.target_labels_dict = None
        if config.target_graph:
            self.target_tokens = load_dict(config.target_dict, model_type)
            _, self.target_labels_dict = parse_transitions(self.target_tokens, self.config.split_transitions)

        # Placeholders
        self.inputs = model_inputs.ModelInputs(config)

        # Convert from time-major to batch-major, handle factors
        self.source_ids, \
            self.source_mask, \
            self.target_ids_in, \
            self.target_ids_out, \
            self.target_mask, \
            self.edges, \
            self.labels = self._convert_inputs(self.inputs)
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
                                                  cross_attn_mask, self.edges, self.labels)

            original_mask_shape = tf.shape(self.target_mask)
            if not SQUASH and self.config.target_graph:
                num_sentences = original_mask_shape[0]
                timesteps = original_mask_shape[1]
                self.target_ids_out = repeat(self.target_ids_out, timesteps, 0)

                # print_ops = []
                # print_ops.append(
                #     tf.compat.v1.Print([], [tf.shape(self.target_ids_out), self.target_ids_out], "target_ids_out", 50,
                #                        200))
                # print_ops.append(
                #     tf.compat.v1.Print([], [tf.shape(self.target_mask), original_mask_shape, self.target_mask],
                #                        "target_mask for loss", 50, 200))
                # with tf.control_dependencies(print_ops):
                self.target_mask = tf.minimum(repeat(self.target_mask, timesteps, 0), tf.tile(tf.eye(timesteps), [num_sentences, 1]))

            print_ops = []
            # logits = tf.compat.v1.Print(logits, [tf.shape(self.target_ids_in)], "target_ids_in", 3)
            # logits = tf.compat.v1.Print(logits, [tf.shape(self.target_ids_out)], "target_ids_out", 3)
            # print_ops.append(tf.compat.v1.Print([], [tf.shape(logits), logits[0,:,0]], "logits shapes", 50, 100))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_ids_out), self.target_ids_out], "target_ids_out", 50, 200))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_mask), original_mask_shape, self.target_mask], "target_mask for loss", 50, 200))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_mask), self.target_mask[...,-3:]], "target_mask ends for loss", 50, 200))
            # print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_ids_in), self.target_ids_in], "target_ids_in", 50, 100))
            with tf.control_dependencies(print_ops):
                logits = logits * 1 #TODO delete

            # Instantiate loss layer(s)
            loss_layer = MaskedCrossEntropy(self.dec_vocab_size,
                                            self.config.label_smoothing,
                                            INT_DTYPE,
                                            FLOAT_DTYPE,
                                            time_major=False,
                                            name='loss_layer')

            print("Try loss with repeated (masked) target ids out")
            masked_loss, sentence_loss, batch_loss = \
                loss_layer.forward(logits, self.target_ids_out, self.target_mask, self.training)
            if self.config.edge_num_constrain > 0:
                if self.config.split_transitions:
                    raise NotImplementedError()
                # print_ops = []
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_ids_in), self.target_ids_in], "target_ids_in", 50, 100))
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(self.target_ids_out), self.target_ids_out], "target_ids_out", 50, 100))
                # with tf.control_dependencies(print_ops):
                constrain = EdgeConstrain(self.dec_vocab_size, self.legal_edge, name='edge_constrain_layer')
                masked_cons, sentence_cons, batch_cons = \
                    constrain.forward(logits, self.target_ids_in, self.target_mask, self.training)
                masked_loss -= masked_cons * self.config.edge_num_constrain
                sentence_loss -= sentence_cons * self.config.edge_num_constrain
                batch_loss -= batch_cons * self.config.edge_num_constrain
            if self.config.inverse_loss:
                inverse_rate = 0.5
                inverse_loss = MaskedCrossEntropy(self.dec_vocab_size,
                                                  self.config.label_smoothing,
                                                  INT_DTYPE,
                                                  FLOAT_DTYPE,
                                                  time_major=False,
                                                  name='inverse_loss_layer')

                inv_masked_loss, inv_sentence_loss, inv_batch_loss = \
                    inverse_loss.forward(logits, self.target_ids_in, self.target_mask, self.training)
                masked_loss -= inv_masked_loss * inverse_rate
                sentence_loss -= inv_sentence_loss * inverse_rate
                batch_loss -= inv_batch_loss * inverse_rate

            # Calculate loss
            if self.config.print_per_token_pro:
                # e**(-(-log(probability))) =  probability
                self._print_pro = tf.math.exp(-masked_loss)

            sent_lens = tf.reduce_sum(input_tensor=self.target_mask, axis=1, keepdims=False)

            print_ops = []
            self._loss_per_sentence = sentence_loss * sent_lens
            self._loss = tf.reduce_mean(input_tensor=self._loss_per_sentence, keepdims=False)
            print_ops.append(tf.compat.v1.Print([], [tf.shape(masked_loss), masked_loss], "masked_loss", 100, 200))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(self._loss), self._loss], "self._loss", 100, 200))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(sentence_loss), sentence_loss], "sentence_loss", 100, 200))
            print_ops.append(tf.compat.v1.Print([], [tf.shape(sent_lens), sent_lens], "sent_lens", 100, 200))
            with tf.control_dependencies(print_ops):
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
            edges = inputs.edges
            edges = tf.sparse.transpose(edges, perm=[len(edges.shape) - 1] + list(range(len(edges.shape) - 1)))
            if self.config.target_labels_num > 0:
                labels = inputs.labels
                labels = tf.sparse.transpose(labels, perm=[len(labels.shape) - 1] + list(range(len(labels.shape) - 1)))
            else:
                labels = None
        else:
            edges = None
            labels = None

        # target_ids_in is a bit more complicated since we need to insert
        # the special <GO> symbol (with value 1) at the start of each sentence
        max_len, batch_size = tf.shape(input=inputs.y)[0], tf.shape(input=inputs.y)[1]
        go_symbols = tf.fill(value=1, dims=[1, batch_size])
        tmp = tf.concat([go_symbols, inputs.y], 0)
        tmp = tmp[:-1, :]
        target_ids_in = tf.transpose(a=tmp, perm=[1,0])
        return (source_ids, source_mask, target_ids_in, target_ids_out,
                target_mask, edges, labels)
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
                 from_rnn=False, labels_dict={}, labels_num=None):

        # Set attributes
        self.config = config
        self.embedding_layer = embedding_layer
        self.softmax_projection_layer = softmax_projection_layer
        self.training = training
        self.name = name
        self.from_rnn = from_rnn
        self.labels_num = labels_num
        # self.labels_dict = labels_dict

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
                    activation=tf.nn.relu, use_bias=self.config.target_labels_num > 0, gate=self.config.target_gcn_gating) #TODO use bias, use gate
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

    def decode_at_train(self, target_ids, enc_output, cross_attn_mask, edges, labels):
        """ Returns the probability distribution over target-side tokens conditioned on the output of the encoder;
         performs decoding in parallel at training time. """
        def _decode_all(target_embeddings):
            """ Decodes the encoder-generated representations into target-side logits in parallel. """
            dec_input = target_embeddings
            # add gcn layers
            if self.config.target_graph:
                for layer_id in range(self.config.target_gcn_layers):
                    orig_input = dec_input
                    if self.config.target_labels_num > 0:
                        inputs = [dec_input, edges, labels]
                    else:
                        inputs = [dec_input, edges]

                    dec_input = self.gcn_stack[layer_id].apply(inputs)
                #     dec_input += orig_input   # residual connection
                # print_ops = []
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_input), dec_input], "dec_input", 50, 100))
                # print_ops.append(tf.compat.v1.Print([], [timesteps], "timesteps", 50, 100))
                # with tf.control_dependencies(print_ops):
                dec_input = dec_input[:, :timesteps, :] # slice tensor to save space
                # make sure slicing works (for enc_output too)

            # TODO make sure timesteps is not repeated when conditionally predicting
            # Propagate inputs through the encoder stack
            dec_output = dec_input
            for layer_id in range(1, self.config.transformer_dec_depth + 1):
                # print_ops = []
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_output), dec_output[0,:,0]], "input to self attn first emb dim", 50, 1000))
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_output), dec_output[0,:,-1]], "input to self attn last emb dim", 50, 1000))
                # print_ops.append(
                #     tf.compat.v1.Print([], [tf.shape(dec_output), dec_output[-1, :, 0]], "last in batch - input to self attn first emb dim", 50,
                #              1000))
                # print_ops.append(
                #     tf.compat.v1.Print([], [tf.shape(dec_output), dec_output[-1, :, -1]], "last in batch - input to self attn last emb dim", 50,
                #              1000))
                # # print_ops.append(tf.compat.v1.Print([], [tf.shape(self_attn_mask), self_attn_mask], "self_attn_mask"))
                # with tf.control_dependencies(print_ops):
                dec_output, _ = self.decoder_stack[layer_id][
                    'self_attn'].forward(dec_output, None, self_attn_mask) # avoid attending sentences with no words and words after the sentence (zeros)
                # print_ops = []
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_output), dec_output[0,:,-5:]], "part of dec_output "+ str(layer_id), 50, 300))
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(enc_output), enc_output[0,:,-5:]], "part of enc_output "+ str(layer_id), 50, 300))
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_output), tf.shape(enc_output), tf.shape(cross_attn_mask)], "dec_output, enc_output, cross_mask cross attention input" + str(layer_id), 50, 100))
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(cross_attn_mask), cross_attn_mask[0,:10],cross_attn_mask[1,:10],cross_attn_mask[-2,:10],cross_attn_mask[-1,:10]], "cross attention" + str(layer_id), 50, 100))
                # with tf.control_dependencies(print_ops):
                dec_output, _ = \
                self.decoder_stack[layer_id]['cross_attn'].forward(
                dec_output, enc_output, cross_attn_mask)
                # print_ops = []
                # print_ops.append(tf.compat.v1.Print([], [tf.shape(dec_output)], "decoded succsessfully", 50, 100))
                # with tf.control_dependencies(print_ops):
                dec_output = self.decoder_stack[
                    layer_id]['ffn'].forward(dec_output)
            return dec_output

        def _prepare_targets():
            """ Pre-processes target token ids before they're passed on as input to the decoder
            for parallel decoding. """

            if self.config.target_graph:
                #padding == self.config.maxlen - tf.shape(target_ids)[1] == self.config.maxlen - tf.shape(positional_signal)
                padding = self.config.maxlen + 1 - timesteps
                # printops = []
                # printops.append(tf.compat.v1.Print([], [tf.shape(target_ids), target_ids[:4,:40]], "target_ids shape and two first in batch", 50, 300))
                # printops.append(tf.compat.v1.Print([], [self.config.maxlen], "maxlen", 300, 50))
                # with tf.control_dependencies(printops):
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
            # printops = []
            # printops.append(
            #     tf.compat.v1.Print([], [tf.shape(dec_output), dec_output], "dec_output", 300, 50))
            # with tf.control_dependencies(printops):
            full_logits = self.softmax_projection_layer.project(dec_output)
            return full_logits

        with tf.compat.v1.variable_scope(self.name):
            # Transpose encoder information in hybrid models
            if self.from_rnn:
                enc_output = tf.transpose(a=enc_output, perm=[1, 0, 2])
                cross_attn_mask = tf.transpose(a=cross_attn_mask, perm=[3, 1, 2, 0])


            # printops = []
            # printops.append(tf.compat.v1.Print([], [tf.shape(enc_output), enc_output], "enc_output unchanged", 300, 50))
            # printops.append(
            #     tf.compat.v1.Print([], [tf.shape(enc_output), enc_output[0, ...],
            #                             enc_output[1, ...]], "enc_output", 300, 50))
            # with tf.control_dependencies(printops):
            target_shape = tf.shape(target_ids)
            batch_size = target_shape[0]
            timesteps = target_shape[-1]
            # printops = []
            # printops.append(
            #     tf.compat.v1.Print([], [timesteps], "timestep changes?", 300, 50))
            # printops.append(tf.compat.v1.Print([], [target_shape], "target shape", 300, 50))
            # printops.append(tf.compat.v1.Print([], [self.config.maxlen], "maxlen", 300, 50))
            # printops.append(tf.compat.v1.Print([], [tf.shape(input=target_ids), target_ids], "target_ids are they like decoded x (if not should decoded x lose the beginning 1=<GO>?)", 300, 50))
            # with tf.control_dependencies(printops):
            self_attn_mask = get_right_context_mask(timesteps)
            positional_signal = get_positional_signal(timesteps,
                                                      self.config.embedding_size,
                                                      FLOAT_DTYPE)
            if self.config.target_graph:
                cross_attn_mask = repeat(cross_attn_mask, timesteps, 0)
                enc_output = repeat(enc_output, timesteps, 0)
                # self_attn_mask = None
                # self_attn_mask = repeat(self_attn_mask, batch_size, 2)

                # with tf.control_dependencies(printops):
                if self.config.sequential:
                    print("sequential")
                    unconditional_attn_mask = tf.tile(self_attn_mask, [batch_size * timesteps, 1, 1, 1])
                self_attn_mask = tf.tile(self_attn_mask, [1, 1, batch_size, 1])
                self_attn_mask = tf.transpose(self_attn_mask, [2, 1, 0, 3])
                # print("BACKWARD ATTENTION ONLY ATTENTION")
                if self.config.sequential:
                    self_attn_mask = tf.minimum(self_attn_mask, unconditional_attn_mask)

                # self_attn_mask = tf.reshape(self_attn_mask, [batch_size  * timesteps, -1, timesteps, 1])
                target_ids = repeat(target_ids, timesteps, 0)
                diagonals_mask = tf.ones([timesteps, timesteps], dtype=target_ids.dtype)
                diagonals_mask = tf.compat.v1.matrix_band_part(diagonals_mask, -1, 0)
                # diagonals_mask = tf.linalg.set_diag(diagonals_mask, tf.zeros(tf.shape(diagonals_mask)[0:-1], dtype = target_ids.dtype))
                diagonals_mask = tf.tile(diagonals_mask, [batch_size, 1])
                target_ids *= diagonals_mask
                # edges = get_all_times(timesteps, edge_times)
                # labels = get_all_times(timesteps, labels_times)
                # edges = get_tensor_from_times(timestep, labels_times)
                # labels = get_tensor_from_times(timestep, labels_times)

                # edges = tf.cast(edge_times, dtype=tf.float32)
                # labels = tf.cast(labels_times, dtype=tf.float32)
                printops = []
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(cross_attn_mask), cross_attn_mask[:,:,:,:10]], "cross_attn_mask", 300, 50))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(unconditional_attn_mask), unconditional_attn_mask[:, :, :5, :5]], "unconditional_attn_mask",
                #                        300, 1000))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(self_attn_mask), self_attn_mask[:, :, :5, :5]], "masked attention", 300, 1000))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(get_right_context_mask(timesteps)), get_right_context_mask(timesteps)[:, :, :, :10]], "unchanged masked attention", 300, 50))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(diagonals_mask), diagonals_mask[:,:10]], "diagonal masks (for targets)", 300, 50))


                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(enc_output), enc_output[0,...],
                #                             enc_output[timesteps, ...]], "enc_output", 300, 50))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(positional_signal), positional_signal[0, ..., :10],
                #                             positional_signal[..., : 10]], "positional_signal", 300, 50))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(cross_attn_mask), cross_attn_mask[0,:,:,:10], cross_attn_mask[timesteps,:,:,:10]], "cross_attn_mask", 300, 50))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(self_attn_mask), self_attn_mask[0, :, :5, :5], self_attn_mask[timesteps, :, :5, :5]], "masked attention", 300, 1000))
                # printops.append(
                #     tf.compat.v1.Print([], [tf.shape(target_ids), target_ids[-1,:10], target_ids[-1 - timesteps,:10]], "target_ids in", 300, 50))
                # with tf.control_dependencies(printops):
                logits = _decoding_function()
                if not SQUASH:
                    print("not squashing loss")
                else:
                    diag = tf.range(timesteps)
                    diag = tf.expand_dims(diag, 1)
                    diag = tf.concat([diag, diag], 1) # [[1,1],[2,2]...[timesteps,timesteps]]
                    diag = repeat(diag, self.config.target_vocab_size, 0)

                    vocab_locs = tf.range(self.config.target_vocab_size)
                    vocab_locs = tf.tile(vocab_locs, [timesteps])
                    vocab_locs = tf.expand_dims(vocab_locs, 1)
                    indices = tf.concat([diag, vocab_locs], 1)
                    indices = tf.tile(indices, [batch_size, 1])

                    printops = []
                    printops.append(
                        tf.compat.v1.Print([], [logits[0,:,:3], logits[1,:,:3], logits[2,:,:3]], "first logits ungathered",
                                           300, 50))
                    printops.append(
                        tf.compat.v1.Print([], [logits[timesteps,:,:3], logits[timesteps + 1,:,:3], logits[2,:,:3]], "second sent logits ungathered",
                                           300, 50))
                    printops.append(
                        tf.compat.v1.Print([], [tf.shape(logits), logits[timesteps - 1,:,:3]], "logits ungathered",
                                           300, 50))
                    printops.append( tf.compat.v1.Print([], [tf.shape(indices)], "indices shape", 300, 100))
                    printops.append( tf.compat.v1.Print([], [timesteps], "timesteps", 300, 100))
                    printops.append(
                        tf.compat.v1.Print([], [batch_size, timesteps, self.config.target_vocab_size], "logits reshaped to",
                                           300, 50))
                    with tf.control_dependencies(printops):
                        logits = tf.gather_nd(logits, indices)


                    # printops = []
                    # vocab_size = self.config.target_vocab_size
                    # printops.append(
                    #     tf.compat.v1.Print([],
                    #                        [tf.shape(indices), indices[0], indices[vocab_size],
                    #                         indices[vocab_size*2],indices[vocab_size*3],indices[vocab_size*4]],
                                           # "indices top gather logits (every vocab size)", 300, 100))
                    # tmp = tf.reshape(logits, [batch_size, timesteps, self.config.target_vocab_size])
                    # printops.append(
                    #     tf.compat.v1.Print([],
                    #                        [tf.shape(tmp), tmp[...,:10]],
                    #                        "logits", 300, 50))
                    # with tf.control_dependencies(printops):
                    logits = tf.reshape(logits, [batch_size, timesteps, self.config.target_vocab_size])
            else:
                logits = _decoding_function()
            printops = []
            printops.append(
                tf.compat.v1.Print([], [tf.shape(logits), logits[0,:,:10]], "final logits",
                                   300, 50))
            with tf.control_dependencies(printops):
                logits = logits * 1 + 0

        return logits
