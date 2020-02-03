# based on https://github.com/borgr/gcn_tf/blob/master/gcn.py
import six
from six.moves import xrange  # pylint: disable=redefined-builtin
import numpy as np

import tensorflow as tf
from tensorflow import expand_dims
from tensorflow import tile
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.util.tf_export import tf_export
from tensorflow.python.util import tf_decorator
# from tensorflow.tf_export import tf_export
from tensorflow.python.layers import base
from tensorflow.python.ops import logging_ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import standard_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import special_math_ops
from tensorflow.python.ops import nn
# from tensorflow.python import math_ops
# from tensorflow.contrib.eager import context

def sparse_tensor_dense_tensordot(sp_a, b, axes, name=None):
    r"""Tensor contraction of a and b along specified axes.
    Tensordot (also known as tensor contraction) sums the product of elements
    from `a` and `b` over the indices specified by `a_axes` and `b_axes`.
    The lists `a_axes` and `b_axes` specify those pairs of axes along which to
    contract the tensors. The axis `a_axes[i]` of `a` must have the same dimension
    as axis `b_axes[i]` of `b` for all `i` in `range(0, len(a_axes))`. The lists
    `a_axes` and `b_axes` must have identical length and consist of unique
    integers that specify valid axes for each of the tensors.
    This operation corresponds to `numpy.tensordot(a, b, axes)`.
    Example 1: When `a` and `b` are matrices (order 2), the case `axes = 1`
    is equivalent to matrix multiplication.
    Example 2: When `a` and `b` are matrices (order 2), the case
    `axes = [[1], [0]]` is equivalent to matrix multiplication.
    Example 3: Suppose that \\(a_{ijk}\\) and \\(b_{lmn}\\) represent two
    tensors of order 3. Then, `contract(a, b, [[0], [2]])` is the order 4 tensor
    \\(c_{jklm}\\) whose entry
    corresponding to the indices \\((j,k,l,m)\\) is given by:
    \\( c_{jklm} = \sum_i a_{ijk} b_{lmi} \\).
    In general, `order(c) = order(a) + order(b) - 2*len(axes[0])`.
    Args:
        a: `SparseTensor` of type `float32` or `float64`.
        b: `Tensor` with the same type as `a`.
        axes: Either a scalar `N`, or a list or an `int32` `Tensor` of shape [2, k].
         If axes is a scalar, sum over the last N axes of a and the first N axes
         of b in order.
         If axes is a list or `Tensor` the first and second row contain the set of
         unique integers specifying axes along which the contraction is computed,
         for `a` and `b`, respectively. The number of axes for `a` and `b` must
         be equal.
        name: A name for the operation (optional).
    Returns:
        A `Tensor` with the same type as `a`.
    Raises:
        ValueError: If the shapes of `a`, `b`, and `axes` are incompatible.
        IndexError: If the values in axes exceed the rank of the corresponding
            tensor.
    """

    def _tensordot_reshape(a, axes, flipped=False):
        """Helper method to perform transpose and reshape for contraction op.
        This method is helpful in reducing `math_tf.tensordot` to `math_tf.matmul`
        using `tf.transpose` and `tf.reshape`. The method takes a
        tensor and performs the correct transpose and reshape operation for a given
        set of indices. It returns the reshaped tensor as well as a list of indices
        necessary to reshape the tensor again after matrix multiplication.
        Args:
            a: `Tensor`.
            axes: List or `int32` `Tensor` of unique indices specifying valid axes of
             `a`.
            flipped: An optional `bool`. Defaults to `False`. If `True`, the method
                assumes that `a` is the second argument in the contraction operation.
        Returns:
            A tuple `(reshaped_a, free_dims, free_dims_static)` where `reshaped_a` is
            the tensor `a` reshaped to allow contraction via `matmul`, `free_dims` is
            either a list of integers or an `int32` `Tensor`, depending on whether
            the shape of a is fully specified, and free_dims_static is either a list
            of integers and None values, or None, representing the inferred
            static shape of the free dimensions
        """
        if a.get_shape().is_fully_defined() and isinstance(axes, (list, tuple)):
            shape_a = a.get_shape().as_list()
            axes = [i if i >= 0 else i + len(shape_a) for i in axes]
            free = [i for i in range(len(shape_a)) if i not in axes]
            free_dims = [shape_a[i] for i in free]
            prod_free = int(np.prod([shape_a[i] for i in free]))
            prod_axes = int(np.prod([shape_a[i] for i in axes]))
            perm = list(axes) + free if flipped else free + list(axes)
            new_shape = [prod_axes, prod_free] if flipped else [prod_free, prod_axes]
            reshaped_a = tf.reshape(tf.transpose(a, perm), new_shape)
            return reshaped_a, free_dims, free_dims
        else:
            if a.get_shape().ndims is not None and isinstance(axes, (list, tuple)):
                shape_a = a.get_shape().as_list()
                axes = [i if i >= 0 else i + len(shape_a) for i in axes]
                free = [i for i in range(len(shape_a)) if i not in axes]
                free_dims_static = [shape_a[i] for i in free]
            else:
                free_dims_static = None
            shape_a = tf.shape(a)
            rank_a = tf.rank(a)
            axes = tf.convert_to_tensor(axes, dtype=tf.int32, name="axes")
            axes = tf.cast(axes >= 0, tf.int32) * axes + tf.cast(
                    axes < 0, tf.int32) * (
                            axes + rank_a)
            free, _ = tf.setdiff1d(tf.range(rank_a), axes)
            free_dims = tf.gather(shape_a, free)
            axes_dims = tf.gather(shape_a, axes)
            prod_free_dims = tf.reduce_prod(free_dims)
            prod_axes_dims = tf.reduce_prod(axes_dims)
            perm = tf.concat([axes_dims, free_dims], 0)
            if flipped:
                perm = tf.concat([axes, free], 0)
                new_shape = tf.stack([prod_axes_dims, prod_free_dims])
            else:
                perm = tf.concat([free, axes], 0)
                new_shape = tf.stack([prod_free_dims, prod_axes_dims])
            reshaped_a = tf.reshape(tf.transpose(a, perm), new_shape)
            return reshaped_a, free_dims, free_dims_static

    def _tensordot_axes(a, axes):
        """Generates two sets of contraction axes for the two tensor arguments."""
        a_shape = a.get_shape()
        if isinstance(axes, tf.compat.integral_types):
            if axes < 0:
                raise ValueError("'axes' must be at least 0.")
            if a_shape.ndims is not None:
                if axes > a_shape.ndims:
                    raise ValueError("'axes' must not be larger than the number of "
                                                     "dimensions of tensor %s." % a)
                return (list(range(a_shape.ndims - axes, a_shape.ndims)),
                                list(range(axes)))
            else:
                rank = tf.rank(a)
                return (range(rank - axes, rank, dtype=tf.int32),
                                range(axes, dtype=tf.int32))
        elif isinstance(axes, (list, tuple)):
            if len(axes) != 2:
                raise ValueError("'axes' must be an integer or have length 2.")
            a_axes = axes[0]
            b_axes = axes[1]
            if isinstance(a_axes, tf.compat.integral_types) and \
                    isinstance(b_axes, tf.compat.integral_types):
                a_axes = [a_axes]
                b_axes = [b_axes]
            if len(a_axes) != len(b_axes):
                raise ValueError(
                        "Different number of contraction axes 'a' and 'b', %s != %s." %
                        (len(a_axes), len(b_axes)))
            return a_axes, b_axes
        else:
            axes = tf.convert_to_tensor(axes, name="axes", dtype=tf.int32)
        return axes[0], axes[1]

    def _sparse_tensordot_reshape(a, axes, flipped=False):
        """Helper method to perform transpose and reshape for contraction op.
        This method is helpful in reducing `math_tf.tensordot` to `math_tf.matmul`
        using `tf.transpose` and `tf.reshape`. The method takes a
        tensor and performs the correct transpose and reshape operation for a given
        set of indices. It returns the reshaped tensor as well as a list of indices
        necessary to reshape the tensor again after matrix multiplication.
        Args:
            a: `Tensor`.
            axes: List or `int32` `Tensor` of unique indices specifying valid axes of
             `a`.
            flipped: An optional `bool`. Defaults to `False`. If `True`, the method
                assumes that `a` is the second argument in the contraction operation.
        Returns:
            A tuple `(reshaped_a, free_dims, free_dims_static)` where `reshaped_a` is
            the tensor `a` reshaped to allow contraction via `matmul`, `free_dims` is
            either a list of integers or an `int32` `Tensor`, depending on whether
            the shape of a is fully specified, and free_dims_static is either a list
            of integers and None values, or None, representing the inferred
            static shape of the free dimensions
        """
        if a.get_shape().is_fully_defined() and isinstance(axes, (list, tuple)):
            shape_a = a.get_shape().as_list()
            axes = [i if i >= 0 else i + len(shape_a) for i in axes]
            free = [i for i in range(len(shape_a)) if i not in axes]
            free_dims = [shape_a[i] for i in free]
            prod_free = int(np.prod([shape_a[i] for i in free]))
            prod_axes = int(np.prod([shape_a[i] for i in axes]))
            perm = list(axes) + free if flipped else free + list(axes)
            new_shape = [prod_axes, prod_free] if flipped else [prod_free, prod_axes]
            reshaped_a = tf.sparse_reshape(tf.sparse_transpose(a, perm), new_shape)
            return reshaped_a, free_dims, free_dims
        else:
            if a.get_shape().ndims is not None and isinstance(axes, (list, tuple)):
                shape_a = a.get_shape().as_list()
                axes = [i if i >= 0 else i + len(shape_a) for i in axes]
                free = [i for i in range(len(shape_a)) if i not in axes]
                free_dims_static = [shape_a[i] for i in free]
            else:
                free_dims_static = None
            shape_a = tf.shape(a)
            rank_a = tf.rank(a)
            axes = tf.convert_to_tensor(axes, dtype=tf.int32, name="axes")
            axes = tf.cast(axes >= 0, tf.int32) * axes + tf.cast(
                    axes < 0, tf.int32) * (
                            axes + rank_a)
            # print(sess.run(rank_a), sess.run(axes))
            free, _ = tf.setdiff1d(tf.range(rank_a), axes)
            free_dims = tf.gather(shape_a, free)
            axes_dims = tf.gather(shape_a, axes)
            printop = tf.Print([],
                               [shape_a, axes, axes_dims, free_dims], "dims", 10, 50)
            with tf.control_dependencies([printop]):
                prod_free_dims = tf.reduce_prod(free_dims)
                prod_axes_dims = tf.reduce_prod(axes_dims)
            # perm = tf.concat([axes_dims, free_dims], 0)
            if flipped:
                perm = tf.concat([axes, free], 0)
                new_shape = tf.stack([prod_axes_dims, prod_free_dims])
            else:
                perm = tf.concat([free, axes], 0)
                new_shape = tf.stack([prod_free_dims, prod_axes_dims])
            transposed = tf.sparse_transpose(a, perm)
            printops = []
            printops.append(tf.Print([], [tf.shape(transposed), tf.shape(new_shape), new_shape], "reshaping", 10, 50))
            printops.append(tf.Print([], [tf.shape(a), a.indices, perm], "originals", 10, 50))
            with tf.control_dependencies(printops):
                reshaped_a = tf.sparse_reshape(transposed, new_shape)

            return reshaped_a, free_dims, free_dims_static

    def _sparse_tensordot_axes(a, axes):
        """Generates two sets of contraction axes for the two tensor arguments."""
        a_shape = a.get_shape()
        if isinstance(axes, tf.compat.integral_types):
            if axes < 0:
                raise ValueError("'axes' must be at least 0.")
            if a_shape.ndims is not None:
                if axes > a_shape.ndims:
                    raise ValueError("'axes' must not be larger than the number of "
                                                     "dimensions of tensor %s." % a)
                return (list(range(a_shape.ndims - axes, a_shape.ndims)),
                                list(range(axes)))
            else:
                rank = tf.rank(a)
                return (range(rank - axes, rank, dtype=tf.int32),
                                range(axes, dtype=tf.int32))
        elif isinstance(axes, (list, tuple)):
            if len(axes) != 2:
                raise ValueError("'axes' must be an integer or have length 2.")
            a_axes = axes[0]
            b_axes = axes[1]
            if isinstance(a_axes, tf.compat.integral_types) and \
                    isinstance(b_axes, tf.compat.integral_types):
                a_axes = [a_axes]
                b_axes = [b_axes]
            if len(a_axes) != len(b_axes):
                raise ValueError(
                        "Different number of contraction axes 'a' and 'b', %s != %s." %
                        (len(a_axes), len(b_axes)))
            return a_axes, b_axes
        else:
            axes = tf.convert_to_tensor(axes, name="axes", dtype=tf.int32)
        return axes[0], axes[1]

    with tf.name_scope(name, "SparseTensorDenseTensordot", [sp_a, b, axes]) as name:
#         a = tf.convert_to_tensor(a, name="a")
        b = tf.convert_to_tensor(b, name="b")
        sp_a_axes, b_axes = _sparse_tensordot_axes(sp_a, axes)
        sp_a_reshape, sp_a_free_dims, sp_a_free_dims_static = _sparse_tensordot_reshape(sp_a, sp_a_axes)
        b_reshape, b_free_dims, b_free_dims_static = _tensordot_reshape(
                b, b_axes, True)
        ab_matmul = tf.sparse_tensor_dense_matmul(sp_a_reshape, b_reshape)
        if isinstance(sp_a_free_dims, list) and isinstance(b_free_dims, list):
            return tf.reshape(ab_matmul, sp_a_free_dims + b_free_dims, name=name)
        else:
            sp_a_free_dims = tf.convert_to_tensor(sp_a_free_dims, dtype=tf.int32)
            b_free_dims = tf.convert_to_tensor(b_free_dims, dtype=tf.int32)
            printops = []
            printops.append(
                tf.Print([], [sp_a_free_dims],
                         "a free", 10, 50))
            printops.append(tf.Print([], [b_free_dims], "b free", 10, 50))
            with tf.control_dependencies(printops):
                product = tf.reshape(
                        ab_matmul, tf.concat([sp_a_free_dims, b_free_dims], 0), name=name)
            if sp_a_free_dims_static is not None and b_free_dims_static is not None:
                product.set_shape(sp_a_free_dims_static + b_free_dims_static)
            return product

@tf_export(v1=['layers.dense'])
class GCN(base.Layer):
    """Densely-connected layer class.
    This layer implements the operation:
    `outputs = activation(inputs * kernel + bias)`
    Where `activation` is the activation function passed as the `activation`
    argument (if not `None`), `kernel` is a weights matrix created by the layer,
    and `bias` is a bias vector created by the layer
    (only if `use_bias` is `True`).
    Arguments:
      units: Integer or Long, dimensionality of the output space.
      activation: Activation function (callable). Set it to None to maintain a
        linear activation.
      use_bias: Boolean, whether the layer uses a bias.
      kernel_initializer: Initializer function for the weight matrix.
        If `None` (default), weights are initialized using the default
        initializer used by `tf.get_variable`.
      bias_initializer: Initializer function for the bias.
      kernel_regularizer: Regularizer function for the weight matrix.
      bias_regularizer: Regularizer function for the bias.
      activity_regularizer: Regularizer function for the output.
      kernel_constraint: An optional projection function to be applied to the
          kernel after being updated by an `Optimizer` (e.g. used to implement
          norm constraints or value constraints for layer weights). The function
          must take as input the unprojected variable and must return the
          projected variable (which must have the same shape). Constraints are
          not safe to use when doing asynchronous distributed training.
      bias_constraint: An optional projection function to be applied to the
          bias after being updated by an `Optimizer`.
      trainable: Boolean, if `True` also add variables to the graph collection
        `GraphKeys.TRAINABLE_VARIABLES` (see `tf.Variable`).
      name: String, the name of the layer. Layers with the same name will
        share weights, but to avoid mistakes we require reuse=True in such cases.
      reuse: Boolean, whether to reuse the weights of a previous layer
        by the same name.
    Properties:
      units: Python integer, dimensionality of the output space.
      edges_label_num: Python integer, dimensionality of the edge label space.
      bias_label_num: Python integer, dimensionality of the bias label space.
      activation: Activation function (callable).
      use_bias: Boolean, whether the layer uses a bias.
      kernel_initializer: Initializer instance (or name) for the kernel matrix.
      bias_initializer: Initializer instance (or name) for the bias.
      kernel_regularizer: Regularizer instance for the kernel matrix (callable)
      bias_regularizer: Regularizer instance for the bias (callable).
      activity_regularizer: Regularizer instance for the output (callable)
      kernel_constraint: Constraint function for the kernel matrix.
      bias_constraint: Constraint function for the bias.
      kernel: Weight matrix (TensorFlow variable or tensor).
      bias: Bias vector, if applicable (TensorFlow variable or tensor).
    """

    def __init__(self, units=None,
                 activation=None,
                 gate=True,
                 use_bias=True,
                 kernel_initializer=None,
                 bias_initializer=init_ops.zeros_initializer(),
                 kernel_regularizer=None,
                 bias_regularizer=None,
                 kernel_constraint=None,
                 bias_constraint=None,
                 gate_kernel_initializer=None,
                 gate_bias_initializer=init_ops.zeros_initializer(),
                 gate_kernel_regularizer=None,
                 gate_bias_regularizer=None,
                 gate_kernel_constraint=None,
                 gate_bias_constraint=None,
                 vertices_num=None,
                 edge_labels_num=None,
                 bias_labels_num=None,
                 sparse_graph=True,
                 # activity_regularizer=None,
                 trainable=True,
                 name=None,
                 **kwargs):
        super(GCN, self).__init__(trainable=trainable, name=name,
                                  # activity_regularizer=activity_regularizer,
                                  **kwargs)

        self.units = units
        self.gate = gate
        self.activation = activation
        self.use_bias = use_bias
        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer
        self.kernel_regularizer = kernel_regularizer
        self.bias_regularizer = bias_regularizer
        # self.kernel_constraint = kernel_constraint
        self.bias_constraint = bias_constraint
        self.gate_kernel_initializer = gate_kernel_initializer
        self.gate_bias_initializer = gate_bias_initializer
        self.gate_kernel_regularizer = gate_kernel_regularizer
        self.gate_bias_regularizer = gate_bias_regularizer
        # self.gate_kernel_constraint = gate_kernel_constraint
        self.gate_bias_constraint = gate_bias_constraint
        self.vert_num = vertices_num
        self.edge_labels_num = edge_labels_num
        self.bias_labels_num = bias_labels_num
        self.sparse_graph = sparse_graph
        self.input_spec = [base.InputSpec(min_ndim=2), base.InputSpec(
            min_ndim=2), base.InputSpec(min_ndim=2)]

    def build(self, input_shape):
        base_input_shape = tensor_shape.TensorShape(input_shape[0])
        # print("base_input_shape", base_input_shape)
        vert_num = base_input_shape[-2].value
        if vert_num is None and self.vert_num is not None:
            vert_num = self.vert_num
        self.vert_num = vert_num
        embed_size = base_input_shape[-1].value
        self.embed_size = embed_size
        if embed_size is None:
            raise ValueError('The second to last dimension of the inputs to `GCN` - the embedding size - '
                             'should be defined. Found `None`.')
        if vert_num is None:
            print("inputshapt!!", input_shape)
            raise ValueError('The last dimensions of the inputs to `GCN` - the number of vertices - '
                             'should be defined. Found `None`.')
        if self.sparse_graph:
            if not self.edge_labels_num:
                raise ValueError('edge_labels_num must be passed if graph is sparsely represented')
            edge_labels_num = self.edge_labels_num
        else:
            edge_shape = tensor_shape.TensorShape(input_shape[1])
            edge_labels_num = edge_shape[-1].value
            if edge_labels_num is None:
                raise ValueError('The last dimension of the edges inputs to `GCN` - the number of edge labels - '
                                 'should be defined. Found `None`.')
        if self.units is None:
            self.units = embed_size

        self.main_input_spec = base.InputSpec(min_ndim=3,
                                              axes={-2: vert_num, -1: embed_size})
        self.kernel = self.add_variable('kernel',
                                        shape=[embed_size, self.units,
                                               edge_labels_num],
                                        initializer=self.kernel_initializer,
                                        regularizer=self.kernel_regularizer,
                                        # constraint=self.kernel_constraint,
                                        dtype=self.dtype,
                                        trainable=True)
        self.gate_kernel = self.add_variable('gate_kernel',
                                             shape=[embed_size,
                                                    edge_labels_num],
                                             initializer=self.gate_kernel_initializer,
                                             regularizer=self.gate_kernel_regularizer,
                                             # constraint=self.gate_kernel_constraint,
                                             dtype=self.dtype,
                                             trainable=True)
        self.edges_spec = base.InputSpec(
            min_ndim=4, axes={-3: vert_num, -2: vert_num, -1: edge_labels_num})
        if self.use_bias:
            if self.sparse_graph:
                if not self.bias_labels_num:
                    raise ValueError('bias_labels_num must be passed if graph is sparsely represented')
                bias_labels_num = self.bias_labels_num
            else:
                bias_shape = tensor_shape.TensorShape(input_shape[2])
                bias_labels_num = bias_shape[-1].value
                if bias_labels_num is None:
                    raise ValueError('The last dimension of the biases inputs to `GCN` '
                                     'should be defined. Found `None`.')
            self.bias_labels_spec = base.InputSpec(
                min_ndim=4, axes={-3: vert_num, -2: vert_num, -1: bias_labels_num})
            self.bias = self.add_variable('bias',
                                          shape=[self.units,
                                                 bias_labels_num],
                                          initializer=self.bias_initializer,
                                          regularizer=self.bias_regularizer,
                                          # constraint=self.bias_constraint,
                                          dtype=self.dtype,
                                          trainable=True)
            self.gate_bias = self.add_variable('gate_bias',
                                               shape=[bias_labels_num],
                                               initializer=self.gate_bias_initializer,
                                               regularizer=self.gate_bias_regularizer,
                                               # constraint=self.gate_bias_constraint,
                                               dtype=self.dtype,
                                               trainable=True)
            self.input_spec = [self.main_input_spec,
                               self.edges_spec, self.bias_labels_spec]
        else:
            self.input_spec = [self.main_input_spec, self.edges_spec]
            self.bias = None
            self.gate_bias = None
        self.built = True

    def calculate_gates(self):
        if self.use_bias:
            # bias_shape = self.bias.get_shape().as_list()
            # bias gate
            if self.sparse_graph:
                # printops = []
                # printops.append(tf.Print([], [tf.shape(self.bias_labels), self.bias_labels.indices], "bias_labels shape, indices", 10, 300))
                # printops.append(tf.Print([], [tf.size(self.bias_labels.indices)], "bias_labels size", 10, 50))
                # printops.append(tf.Print([], [tf.shape(self.gate_bias)], "gate_bias", 10, 50))
                # with tf.control_dependencies(printops):
                biases = sparse_tensor_dense_tensordot(self.bias_labels, self.gate_bias, [[-1], [-1]])
            else:
                biases = math_ops.reduce_sum(
                    math_ops.multiply(self.gate_bias, self.bias_labels), [-1])
            # print("gate bias shape", biases.get_shape().as_list())

        # per neighbor, per label gating scalar
        printops = []
        printops.append(tf.Print([], [tf.shape(self.x), tf.shape(self.gate_kernel)], "gate_kernel shapes", 10, 300))
        with tf.control_dependencies(printops):
            xw = standard_ops.tensordot(self.x, self.gate_kernel, tf.constant([[-1], [0]], dtype=tf.int32))
        xw = expand_dims(xw, 2)

        # main gate
        if self.sparse_graph:
            res_shape = self.labels.dense_shape #tf.cast(tf.shape(self.labels), tf.int64)
            gate_indices = self.labels.indices
            printops = []
            printops.append(
                tf.Print([], [tf.shape(gate_indices), gate_indices], "gate_indices", 10, 300))
            with tf.control_dependencies(printops):
                gate_indices = tf.constant([1,1,0,1], dtype=gate_indices.dtype) * gate_indices # gates care only about first label

            printops = []
            printops.append(
                tf.Print([], [tf.shape(self.labels.indices), self.labels.indices], "gate_labels", 10, 300))
            printops.append(tf.Print([], [tf.shape(xw), xw], "gate_xw", 10, 50))
            printops.append(tf.Print([], [tf.shape(self.labels)], "labels_xw", 10, 50))
            printops.append(tf.Print([], [tf.shape(tf.gather_nd(xw, gate_indices))], "gate_gathered", 10, 50))
            with tf.control_dependencies(printops):
                res_vals = tf.gather_nd(xw, gate_indices) # * self.labels.values
            sparse_xw = tf.SparseTensor(self.labels.indices, res_vals, res_shape)

            printops = []
            printops.append(tf.Print([], [tf.shape(sparse_xw)], "sparse_xw shape", 10, 50))
            printops.append(tf.Print([], [sparse_xw.indices], "sparse_xw indices", 10, 50))
            printops.append(tf.Print([], [sparse_xw.values], "sparse_xw vals", 10, 50))
            with tf.control_dependencies(printops):
                edges = tf.sparse.reduce_sum(sparse_xw, axis=-1)
        else:
            edges = math_ops.reduce_sum(
                math_ops.multiply(xw, self.labels), [-1])

        # combine two scalar gates (per neighbor per vertex)
        if self.use_bias:
            printops = []
            printops.append(tf.Print([], [tf.shape(biases)], "adding in gates", 10, 300))
            with tf.control_dependencies(printops):
                out = edges + biases
        else:
            out = edges
        printops = []
        printops.append(tf.Print([], [tf.shape(out)],  "gate_done", 10, 300))
        # printops.append(tf.Print([], [out.indices],  "gate_done", 10, 300))
        # printops.append(
        #     tf.Print([], [math_ops.reduce_sum(out, [1]), out],  "output_done", 10, 300))
        with tf.control_dependencies(printops):
            gates = math_ops.sigmoid(out)
        return gates

    def kernel_by_sparse(self, kernel, sparse):
        """
        multiplies a dense kernel tensor by a sparse tensor. kernel is of size [batch_size, max_len, 1, embedding, edge_type_num] and sparse [batch_size, max_len, max_len, edge_type_num=3]
        returns the value after multiplication and after summing out last sparse dimension (labels)
        :param kernel:
        :param sparse:
        :return:
        """
        kernel_shape = tf.shape(kernel, out_type=tf.int64)
        embedding_size = kernel_shape[-1]
        values_len = tf.shape(sparse.indices, out_type=tf.int64)[0]

        # expand sparse
        res_shape = kernel_shape

        embed_dim = tf.sort(tf.tile(tf.range(embedding_size), [values_len]))  # 1 to embedding size tensor to add to indices (form: [1,1,1...2,2,2...])
        embed_dim = tf.reshape(embed_dim, [-1,1]) # reshape for concat)
        # embed_dim = tf.cast(embed_dim, tf.int64)

        indices = sparse.indices
        res_idx = tf.tile(indices, [embedding_size, 1]) # duplicate indices embedding size times

        res_idx = tf.concat([res_idx, embed_dim], 1)
        # printops = []
        # printops.append(tf.Print([], [tf.shape(res_idx), res_idx], "res_idx", 10, 50))
        # with tf.control_dependencies(printops):
        ker_idx = tf.constant([1, 1, 0, 1, 1],
                                       dtype=res_idx.dtype) * res_idx  # xw does not have dimension number 2 #TODO finish adding this
        res_vals = tf.tile(sparse.values, [embedding_size])

        # multiply by xw
        res_vals = tf.gather_nd(kernel, ker_idx) * res_vals
        sparse_kernel = tf.SparseTensor(res_idx, res_vals, res_shape)
        outputs = tf.sparse.reduce_sum(sparse_kernel, axis=-1)
        return outputs

    def calculate_kernel(self):

        shape = tf.shape(self.x)

        # printops = []
        # printops.append(tf.Print([], [self.x], "x", 10, 50))
        # printops.append(tf.Print([], [self.kernel], "kernel", 10, 50))
        # printops.append(tf.Print([], [tf.shape(self.x), tf.shape(self.kernel)], "shape main_kernel", 10, 50))
        # with tf.control_dependencies(printops):
        xw = standard_ops.tensordot(self.x, self.kernel, tf.constant([[-1], [0]], dtype=tf.int32))

        # broadcast for each neighbor
        xw = expand_dims(xw, 2)

        if self.sparse_graph:
            outputs = self.kernel_by_sparse(xw, self.labels)
        else:
            xw = tile(xw, [1, 1, shape[-2], 1, 1])
            labeled_edges = expand_dims(self.labels, -2)
            outputs = math_ops.reduce_sum(math_ops.multiply(xw, labeled_edges), [-1])
        return outputs

    def calculate_bias(self):
        # print("biases shapes", self.bias.get_shape().as_list(), self.bias_labels.get_shape().as_list())
        if self.sparse_graph:
            # labeled_bias = self.kernel_by_sparse(self.bias, self.bias_labels)
            printops = []
            printops.append(tf.Print([], [tf.shape(self.bias_labels)], "shape bias_labels", 10, 50))
            printops.append(tf.Print([], [self.bias_labels.dense_shape], "dense_shap", 10, 50))
            printops.append(tf.Print([], [self.bias_labels.values], "values bias_labels", 10, 50))
            printops.append(tf.Print([], [self.bias_labels.indices], "indicesbias_labels", 10, 50))
            printops.append(tf.Print([], [tf.shape(self.bias), self.bias], "bias", 10, 50))
            with tf.control_dependencies(printops):
                labeled_bias = sparse_tensor_dense_tensordot(self.bias_labels, self.bias, [[-1], [-1]])
                # labeled_bias = tf.contrib.layers.dense_to_sparse(labeled_bias)
        else:
            labeled_bias = standard_ops.tensordot(self.bias_labels, self.bias, [[-1], [-1]])
        return labeled_bias

    def call(self, inputs,  *args, **kwargs):
        return self._helper(inputs)

    def _helper(self, inputs):
        self.x = ops.convert_to_tensor(inputs[0], dtype=self.dtype)
        if self.sparse_graph:
            self.labels = inputs[1]
        else:
            self.labels = math_ops.cast(ops.convert_to_tensor(inputs[1]), self.dtype)

        print_ops = []
        # print_ops.append(tf.Print([], [tf.shape(inputs[1]), inputs[1].indices[-100:]], "edges input", 10, 100))
        print_ops.append(tf.Print([], [tf.shape(inputs[0]), inputs[0][0, :, 0]], "x input shape and first sent", 10, 50))
        with tf.control_dependencies(print_ops):
            outputs = self.calculate_kernel()

        if self.use_bias:
            if self.sparse_graph:
                print_op = tf.Print([], [tf.shape(inputs[2]), inputs[2].indices, inputs[2].values], "biases origin", 10, 50)
                with tf.control_dependencies([print_op]):
                    self.bias_labels = inputs[2]
                # outputs = tf.sparse.add(outputs, self.calculate_bias()) # TODO option b map_fn
            else:
                self.bias_labels = math_ops.cast(ops.convert_to_tensor(inputs[2]), self.dtype)
            outputs = outputs + self.calculate_bias()

        if self.gate:
            gates = self.calculate_gates()
            # printops = []
            # printops.append(tf.Print([], [tf.shape(gates), gates], "gate_out", 10, 50))
            # printops.append(tf.Print([], [tf.shape(outputs), outputs.indices, outputs.values], "general_out", 10, 1000))
            # printops.append(tf.Print([], [tf.shape(outputs), outputs[:, :2, :2, :10]], "general_out", 10, 1000))
            # with tf.control_dependencies(printops):
            gates = expand_dims(gates, -1)
            # gates = tf.tile(gates, [1, 1, 1, tf.shape(outputs)[-1]])
            if self.sparse_graph:

                outputs.set_shape([None, None, None, None])
                outputs = tf.contrib.layers.dense_to_sparse(outputs)
                # multiply without tiling or converting outputs to dense
                gate_indices = outputs.indices * [1,1,1,0]
                # printops = []
                # printops.append(tf.Print([], [tf.shape(gates), gates], "gate_out", 10, 50))
                # tmp = tf.contrib.layers.dense_to_sparse(gates - 0.5)
                # printops.append(tf.Print([], [tf.shape(tmp.indices), tmp.indices, tmp.values], "non .5 in gate", 10, 50))
                # printops.append(tf.Print([], [tf.shape(outputs), tf.shape(outputs.indices), outputs.indices, outputs.values], "general_out", 10, 1000))
                # printops.append(tf.Print([], [tf.shape(outputs), outputs[:, :2, :2, :10]], "general_out", 10, 1000))
                # with tf.control_dependencies(printops):
                gated_vals = tf.gather_nd(gates, gate_indices) * outputs.values
                outputs = tf.SparseTensor(outputs.indices, gated_vals, outputs.dense_shape)# tf.cast(tf.shape(self.labels), tf.int64)
                # printops = []
                # printops.append(tf.Print([], [tf.shape(outputs), tf.size(outputs), outputs.indices, outputs.values], "towards reduce_sum", 10, 50))
                # with tf.control_dependencies(printops):
                outputs = tf.sparse.reduce_sum(outputs, axis = -2)
            else:
                outputs = math_ops.multiply(gates, outputs)
                outputs = math_ops.reduce_sum(outputs, [-2])

        out_shape = [-1, self.vert_num, self.embed_size]

        printops = []
        # printops.append(tf.Print([], [out_shape], "towards rehsaping to out_shape", 10, 50))
        printops.append(tf.Print([], [tf.shape(outputs), tf.math.count_nonzero(outputs)], "towards rehsaping outputs", 10, 50))
        with tf.control_dependencies(printops):
            outputs = tf.reshape(outputs, out_shape)

        if self.activation is not None:
            return self.activation(outputs)  # pylint: disable=not-callable
        return outputs

    def compute_output_shape(self, input_shape):
        input_shape = tensor_shape.TensorShape(input_shape)
        input_shape = input_shape.with_rank_at_least(2)
        if input_shape[-1].value is None:
            raise ValueError(
                'The innermost dimension of input_shape must be defined, but saw: %s'
                % input_shape)
        return input_shape[0][:-1].concatenate(self.units)

def sparse_dense_matmult_batch(sp_a, b):

    def map_function(x):
        i, dense_slice = x[0], x[1]
        sparse_slice = tf.sparse.reshape(tf.sparse.slice(
            sp_a, [i, 0, 0], [1, sp_a.dense_shape[1], sp_a.dense_shape[2]]),
            [sp_a.dense_shape[1], sp_a.dense_shape[2]])
        mult_slice = tf.sparse.matmul(sparse_slice, dense_slice)
        return mult_slice

    elems = (tf.range(0, sp_a.dense_shape[0], delta=1, dtype=tf.int64), b)
    return tf.map_fn(map_function, elems, dtype=tf.float32, back_prop=True)

@tf_export(v1=['layers.dense'])
def gcn(
        inputs, units=None,
        activation=None,
        gate=True,
        use_bias=True,
        kernel_initializer=None,
        bias_initializer=init_ops.zeros_initializer(),
        kernel_regularizer=None,
        bias_regularizer=None,
        kernel_constraint=None,
        bias_constraint=None,
        gate_kernel_initializer=None,
        gate_bias_initializer=init_ops.zeros_initializer(),
        gate_kernel_regularizer=None,
        gate_bias_regularizer=None,
        gate_kernel_constraint=None,
        gate_bias_constraint=None,
        vertices_num=None,
        edge_labels_num=None,
        bias_labels_num=None,
        sparse_graph=True,
        # activity_regularizer=None,
        trainable=True,
        name=None,
        reuse=None):
    """Functional interface for the graph convolutional network.
    This layer implements the operation:
    `outputs = activation(inputs.labeled_graph_kernel + labeled_bias)`
    Where `activation` is the activation function passed as the `activation`
    argument (if not `None`), `kernel` is a weights matrix created by the layer,
    a different matrix per label,
    and `bias` is a bias vector created by the layer, a different bias per label
    (only if `use_bias` is `True`).
    Arguments:
      inputs: List of Tensor inputs.
      The inputs, the edges labels and the bias labels.
      Labels are expected in the form of neighbors X vertices X labels tensors
      with 0 or 1 representing the existence of a labeled edge between a vertice to its neighbor.
      units: Integer or Long, dimensionality of the output space.
      activation: Activation function (callable). Set it to None to maintain a
        linear activation.
      use_bias: Boolean, whether the layer uses a bias.
      kernel_initializer: Initializer function for the weight matrix.
        If `None` (default), weights are initialized using the default
        initializer used by `tf.get_variable`.
      bias_initializer: Initializer function for the bias.
      kernel_regularizer: Regularizer function for the weight matrix.
      bias_regularizer: Regularizer function for the bias.
      activity_regularizer: Regularizer function for the output.
      kernel_constraint: An optional projection function to be applied to the
          kernel after being updated by an `Optimizer` (e.g. used to implement
          norm constraints or value constraints for layer weights). The function
          must take as input the unprojected variable and must return the
          projected variable (which must have the same shape). Constraints are
          not safe to use when doing asynchronous distributed training.
      bias_constraint: An optional projection function to be applied to the
          bias after being updated by an `Optimizer`.
      trainable: Boolean, if `True` also add variables to the graph collection
        `GraphKeys.TRAINABLE_VARIABLES` (see `tf.Variable`).
      name: String, the name of the layer.
      reuse: Boolean, whether to reuse the weights of a previous layer
        by the same name.
    Returns:
      Output tensor the same shape as `inputs` except the last dimension is of
      size `units`.
    Raises:
      ValueError: if eager execution is enabled.
    """
    layer = GCN(units,
                activation=activation,
                gate=gate,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                gate_kernel_initializer=gate_kernel_initializer,
                gate_bias_initializer=init_ops.zeros_initializer(),
                gate_kernel_regularizer=gate_kernel_regularizer,
                gate_bias_regularizer=gate_bias_regularizer,
                gate_kernel_constraint=gate_kernel_constraint,
                gate_bias_constraint=gate_bias_constraint,
                edge_labels_num=edge_labels_num,
                bias_labels_num=bias_labels_num,
                sparse_graph=sparse_graph,
                kernel_constraint=kernel_constraint,
                bias_constraint=bias_constraint,
                # activity_regularizer=activity_regularizer,
                trainable=trainable,
                name=name,
                # dtype=inputs[0].dtype.base_dtype,
                _scope=name,
                _reuse=reuse
                )

    return layer.apply(inputs)
