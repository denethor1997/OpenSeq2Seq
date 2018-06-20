"""Implementation of a 1d convolutional layer with weight normalization."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf


class Conv1DNetworkNormalized(tf.layers.Layer):
  """1D convolutional layer with weight normalization"""
  """ Inspired from https://github.com/tobyyouup/conv_seq2seq"""

  def __init__(self, in_dim, out_dim, kernel_width, mode, layer_id, hidden_dropout, conv_padding, decode_padding):
    super(Conv1DNetworkNormalized, self).__init__()
    self.mode = mode
    self.conv_padding = conv_padding
    self.decode_padding = decode_padding
    self.hidden_dropout = hidden_dropout
    self.kernel_width = kernel_width

    with tf.variable_scope("conv_layer_" + str(layer_id)):
      # use weight normalization (Salimans & Kingma, 2016)  w = g * v/2-norm(v)
      self.V = tf.get_variable('V', shape=[kernel_width, in_dim, 2*out_dim], dtype=tf.float32,
                          initializer=tf.random_normal_initializer(mean=0, stddev=tf.sqrt(
                            4.0 * hidden_dropout / (kernel_width * in_dim))), trainable=True)
      self.V_norm = tf.norm(self.V.initialized_value(), axis=[0, 1])
      self.g = tf.get_variable('g', dtype=tf.float32, initializer=self.V_norm, trainable=True)
      self.b = tf.get_variable('b', shape=[2*out_dim], dtype=tf.float32, initializer=tf.zeros_initializer(),
                               trainable=True)

      self.W = tf.reshape(self.g, [1, 1, 2*out_dim]) * tf.nn.l2_normalize(self.V, [0, 1])

  def call(self, input):
    x = input
    if self.mode == "train":
      x = tf.nn.dropout(x, self.hidden_dropout)

    if self.decode_padding:
      x = tf.pad(x, [[0, 0], [self.kernel_width - 1, self.kernel_width - 1], [0, 0]], "CONSTANT")

    output = tf.nn.bias_add(tf.nn.conv1d(value=x, filters=self.W, stride=1, padding=self.conv_padding), self.b)

    if self.decode_padding and self.kernel_width > 1:
        output = output[:, 0:-self.kernel_width + 1, :]

    output = self.gated_linear_units(output)

    return output

  def gated_linear_units(self, inputs):
    input_shape = inputs.get_shape().as_list()
    assert len(input_shape) == 3
    input_pass = inputs[:, :, 0:int(input_shape[2] / 2)]
    input_gate = inputs[:, :, int(input_shape[2] / 2):]
    input_gate = tf.sigmoid(input_gate)
    return tf.multiply(input_pass, input_gate)