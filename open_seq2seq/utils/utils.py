# Copyright (c) 2017 NVIDIA Corporation
from __future__ import absolute_import, division, print_function
from __future__ import unicode_literals
from six.moves import range
from six import string_types

import tensorflow as tf
import subprocess
import numpy as np
import time
from tensorflow.python.client import device_lib


def get_available_gpus():
  # WARNING: this method will take all the memory on all devices!
  local_device_protos = device_lib.list_local_devices()
  return [x.name for x in local_device_protos if x.device_type == 'GPU']


def clip_sparse(value, size):
  dense_shape_clipped = value.dense_shape
  dense_shape_clipped[0] = size
  indices_clipped = []
  values_clipped = []
  for idx_tuple, val in zip(value.indices, value.values):
    if idx_tuple[0] < size:
      indices_clipped.append(idx_tuple)
      values_clipped.append(val)
  return tf.SparseTensorValue(np.array(indices_clipped),
                              np.array(values_clipped),
                              dense_shape_clipped)


def clip_last_batch(last_batch, true_size):
  last_batch_clipped = []
  for val in last_batch:
    if isinstance(val, tf.SparseTensorValue):
      last_batch_clipped.append(clip_sparse(val, true_size))
    else:
      last_batch_clipped.append(val[:true_size])
  return last_batch_clipped


def iterate_data_layer(model, dl_id, sess, compute_loss, mode, verbose):
  total_time = 0.0
  bench_start = model.params.get('bench_start', 10)
  results_per_batch = []

  if model.on_horovod:
    data_layer = model.get_data_layer()
    if compute_loss:
      loss_tensor = model.eval_losses[0]
    output_tensors = model.get_output_tensors()
  else:
    data_layer = model.get_data_layer(dl_id)
    if compute_loss:
      loss_tensor = model.eval_losses[dl_id]
    output_tensors = model.get_output_tensors(dl_id)

  sess.run(data_layer.iterator.initializer)

  fetches = [
    data_layer.input_tensors,
    output_tensors,
  ]

  if compute_loss:
    fetches.append(loss_tensor)
    total_loss = 0.0
    total_samples = 0.0

  size_defined = data_layer.get_size_in_samples() is not None

  if size_defined:
    data_size = data_layer.get_size_in_samples() // \
                data_layer.params['batch_size']
    last_batch_size = data_layer.get_size_in_samples() % \
                      data_layer.params['batch_size']

  if model.on_horovod:
    worker_id = model.hvd.rank()
  else:
    worker_id = dl_id

  cross_over = 0
  if size_defined:
    if data_size == 0:
      raise ValueError(
        "Batch size is bigger than dataset size: {} > {}".format(
          data_layer.params['batch_size'], data_layer.get_size_in_samples()
        )
      )
    if last_batch_size != 0:
      cross_over = 1
  else:
    # setting data_size to be infinity and assume
    # that tf.errors.OutOfRangeError will be raised
    data_size = 1000000000000

  for step in range(data_size + cross_over):
    tm = time.time()
    try:
      if compute_loss:
        inputs, outputs, loss = sess.run(fetches)
      else:
        inputs, outputs = sess.run(fetches)
    except tf.errors.OutOfRangeError:
      break
    if step >= bench_start:
      total_time += time.time() - tm

    # assuming any element of inputs["source_tensors"][ shape[0] is batch size
    batch_size = inputs["source_tensors"][0].shape[0]

    if compute_loss:
      total_loss += loss * batch_size
      total_samples += batch_size

    if size_defined and step == data_size:
      inputs["source_tensors"] = model.clip_last_batch(
        inputs["source_tensors"], last_batch_size,
      )
      if 'target_tensors' in inputs:
        inputs["target_tensors"] = model.clip_last_batch(
          inputs["target_tensors"], last_batch_size,
        )
      outputs = model.clip_last_batch(outputs, last_batch_size)

    if mode == 'eval':
      results_per_batch.append(model.evaluate(inputs, outputs))
    elif mode == 'infer':
      results_per_batch.append(model.infer(inputs, outputs))
    else:
      raise ValueError("Unknown mode: {}".format(mode))

    if verbose:
      if size_defined:
        if data_size > 10 and step % (data_size // 10) == 0:
          deco_print("Processed {}/{} batches on worker {}".format(
            step + 1, data_size, worker_id))
      else:
        deco_print("Processed {} batches".format(step + 1), end='\r')

  if verbose:
    if step > bench_start:
      deco_print(
        "Avg time per step: {:.3}s on worker {}".format(
          1.0 * total_time / (step - bench_start), worker_id),
      )
    else:
      deco_print(
        "Not enough steps for benchmarking on worker {}".format(worker_id)
      )

  if compute_loss:
    return results_per_batch, total_loss, total_samples
  else:
    return results_per_batch


def get_results_for_epoch(model, sess, compute_loss, mode, verbose=False):
  if model.on_horovod:
    if compute_loss:
      results_per_batch, total_loss, total_samples = iterate_data_layer(
        model, 0, sess, compute_loss, mode, verbose,
      )
    else:
      results_per_batch = iterate_data_layer(
        model, 0, sess, compute_loss, mode, verbose,
      )
  else:
    results_per_batch_all = []
    total_loss_all = []
    total_samples_all = []
    for dl_id in range(model.num_gpus):
      if compute_loss:
        results_per_batch, total_loss, total_samples = iterate_data_layer(
          model, dl_id, sess, compute_loss, mode, verbose,
        )
        total_loss_all.append(total_loss)
        total_samples_all.append(total_samples)
      else:
        results_per_batch = iterate_data_layer(
          model, dl_id, sess, compute_loss, mode, verbose,
        )
      results_per_batch_all.append(results_per_batch)

  if model.on_horovod:
    import mpi4py.rc
    mpi4py.rc.initialize = False
    from mpi4py import MPI

    if compute_loss:
      total_samples_all = MPI.COMM_WORLD.gather(total_samples)
      total_loss_all = MPI.COMM_WORLD.gather(total_loss)
    results_per_batch_all = MPI.COMM_WORLD.gather(results_per_batch)

    MPI.COMM_WORLD.Barrier()
    if MPI.COMM_WORLD.Get_rank() != 0:
      # returning dummy tuple of correct shape
      if compute_loss:
        return None, None
      else:
        return None

  if compute_loss:
    total_loss = np.sum(total_loss_all)
    total_samples = np.sum(total_samples_all)
  # moving GPU dimension into the batch dimension
  results_per_batch = [item for sl in results_per_batch_all for item in sl]

  if compute_loss:
    total_loss /= total_samples
    return results_per_batch, total_loss

  return results_per_batch


def log_summaries_from_dict(dict_to_log, output_dir, step):
  # this returns the same writer as was created by
  # the first call to this function
  sm_writer = tf.summary.FileWriterCache.get(output_dir)
  for tag, value in dict_to_log.items():
    sm_writer.add_summary(
      tf.Summary(value=[tf.Summary.Value(tag=tag, simple_value=value)]),
      global_step=step,
    )
    sm_writer.flush()


def get_git_hash():
  try:
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                   stderr=subprocess.STDOUT).decode()
  except subprocess.CalledProcessError as e:
    return "{}\n".format(e.output.decode("utf-8"))


def get_git_diff():
  try:
    return subprocess.check_output(['git', 'diff'],
                                   stderr=subprocess.STDOUT).decode()
  except subprocess.CalledProcessError as e:
    return "{}\n".format(e.output.decode("utf-8"))


class Logger(object):
  def __init__(self, stream, log_file):
    self.stream = stream
    self.log = log_file

  def write(self, msg):
    self.stream.write(msg)
    self.log.write(msg)

  def flush(self):
    self.stream.flush()
    self.log.flush()


def flatten_dict(dct):
  flat_dict = {}
  for key, value in dct.items():
    if isinstance(value, int) or isinstance(value, float) or \
       isinstance(value, string_types) or isinstance(value, bool):
      flat_dict.update({key: value})
    elif isinstance(value, dict):
      flat_dict.update(
        {key + '/' + k: v for k, v in flatten_dict(dct[key]).items()})
  return flat_dict


def nest_dict(flat_dict):
  nst_dict = {}
  for key, value in flat_dict.items():
    nest_keys = key.split('/')
    cur_dict = nst_dict
    for i in range(len(nest_keys) - 1):
      if nest_keys[i] not in cur_dict:
        cur_dict[nest_keys[i]] = {}
      cur_dict = cur_dict[nest_keys[i]]
    cur_dict[nest_keys[-1]] = value
  return nst_dict


def nested_update(org_dict, upd_dict):
  for key, value in upd_dict.items():
    if isinstance(value, dict):
      nested_update(org_dict[key], value)
    else:
      org_dict[key] = value


def mask_nans(x):
  x_zeros = tf.zeros_like(x)
  x_mask = tf.is_finite(x)
  y = tf.where(x_mask, x, x_zeros)
  return y


def deco_print(line, offset=0, start="*** ", end='\n'):
  print(start + " " * offset + line, end=end)


def array_to_string(row, vocab, delim=' '):
  n = len(vocab)
  return delim.join(map(lambda x: vocab[x], [r for r in row if 0 <= r < n]))


def text_ids_to_string(row, vocab, S_ID, EOS_ID, PAD_ID,
                       ignore_special=False, delim=' '):
  """For _-to-text outputs this function takes a row with ids,
  target vocabulary and prints it as a human-readable string
  """
  n = len(vocab)
  if ignore_special:
    f_row = []
    for i in range(0, len(row)):
      char_id = row[i]
      if char_id == EOS_ID:
        break
      if char_id != PAD_ID and char_id != S_ID:
        f_row += [char_id]
    return delim.join(map(lambda x: vocab[x], [r for r in f_row if 0 < r < n]))
  else:
    return delim.join(map(lambda x: vocab[x], [r for r in row if 0 < r < n]))


def check_params(config, required_dict, optional_dict):
  if required_dict is None or optional_dict is None:
    return

  for pm, vals in required_dict.items():
    if pm not in config:
      raise ValueError("{} parameter has to be specified".format(pm))
    else:
      if vals == str:
        vals = string_types
      if vals and isinstance(vals, list) and config[pm] not in vals:
        raise ValueError("{} has to be one of {}".format(pm, vals))
      if vals and not isinstance(vals, list) and not isinstance(config[pm], vals):
        raise ValueError("{} has to be of type {}".format(pm, vals))

  for pm, vals in optional_dict.items():
    if vals == str:
      vals = string_types
    if pm in config:
      if vals and isinstance(vals, list) and config[pm] not in vals:
        raise ValueError("{} has to be one of {}".format(pm, vals))
      if vals and not isinstance(vals, list) and not isinstance(config[pm], vals):
        raise ValueError("{} has to be of type {}".format(pm, vals))

  for pm in config:
    if pm not in required_dict and pm not in optional_dict:
      raise ValueError("Unknown parameter: {}".format(pm))


def cast_types(input_dict, dtype):
  cast_input_dict = {}
  for key, value in input_dict.items():
    if isinstance(value, tf.Tensor):
      if value.dtype == tf.float16 or value.dtype == tf.float32:
        if value.dtype.base_dtype != dtype.base_dtype:
          cast_input_dict[key] = tf.cast(value, dtype)
          continue
    if isinstance(value, dict):
      cast_input_dict[key] = cast_types(input_dict[key], dtype)
      continue
    if isinstance(value, list):
      cur_list = []
      for nest_value in value:
        if isinstance(nest_value, tf.Tensor):
          if nest_value.dtype == tf.float16 or nest_value.dtype == tf.float32:
            if nest_value.dtype.base_dtype != dtype.base_dtype:
              cur_list.append(tf.cast(nest_value, dtype))
              continue
        cur_list.append(nest_value)
      cast_input_dict[key] = cur_list
      continue
    cast_input_dict[key] = input_dict[key]
  return cast_input_dict
