# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
import contextlib
import os
import shutil
from dataclasses import dataclass
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest
import tensorflow as tf

from aimet_tensorflow.keras.auto_quant import AutoQuant
from aimet_tensorflow.keras.quantsim import QuantizationSimModel


@pytest.fixture()
def flag_variables():
    return {
        "applied_bn_folding": False,
        "applied_cle": False,
        "applied_adaround": False
    }


@pytest.fixture(scope="session")
def model():
    inputs = tf.keras.Input(shape=(32, 32, 3,))
    x = tf.keras.layers.Conv2D(32, (3, 3))(inputs)
    outputs = tf.keras.layers.Dense(10, activation="softmax")(x)
    functional_model = tf.keras.Model(inputs=inputs, outputs=outputs)
    return functional_model


@pytest.fixture(scope="session")
def dataset_length():
    return 2


@pytest.fixture(scope="session")
def unlabeled_dataset(dataset_length):
    dummy_inputs = tf.random.normal((dataset_length, 16, 16, 3))
    dataset = tf.data.Dataset.from_tensor_slices(dummy_inputs)
    dataset = dataset.batch(1)
    return dataset


def assert_applied_techniques(
        acc, encoding_path, flag_variables,
        target_acc, bn_folded_acc
):
    # Batchnorm folding is always applied.
    assert flag_variables["applied_bn_folding"]

    # If accuracy is good enough after batchnorm folding
    if bn_folded_acc >= target_acc:
        assert acc == bn_folded_acc
        assert encoding_path.endswith("batchnorm_folding.encodings")
        assert not flag_variables["applied_cle"]
        assert not flag_variables["applied_adaround"]
        return


FP32_ACC = 80.0


@contextlib.contextmanager
def patch_ptq_techniques(bn_folded_acc, cle_acc, adaround_acc, flag_variables):
    def bn_folding(model: tf.keras.Model, *_, **__):
        flag_variables["applied_bn_folding"] = True
        return tuple()

    class _QuantizationSimModel(QuantizationSimModel):
        def compute_encodings(self, *_):
            pass

        def set_and_freeze_param_encodings(self, _):
            pass

    def mock_eval_callback(model, _):
        if flag_variables["applied_adaround"]:
            return adaround_acc
        if flag_variables["applied_cle"]:
            return cle_acc
        if flag_variables["applied_bn_folding"]:
            return bn_folded_acc

        return FP32_ACC

    @dataclass
    class Mocks:
        eval_callback: Callable
        QuantizationSimModel: MagicMock
        fold_all_batch_norms: MagicMock

    with patch("aimet_tensorflow.keras.auto_quant.QuantizationSimModel",
               side_effect=_QuantizationSimModel) as mock_qsim, \
            patch("aimet_tensorflow.keras.auto_quant.fold_all_batch_norms", side_effect=bn_folding) as mock_bn_folding:
        try:
            yield Mocks(eval_callback=mock_eval_callback,
                        QuantizationSimModel=mock_qsim,
                        fold_all_batch_norms=mock_bn_folding)
        finally:
            pass


@pytest.fixture(autouse=True)
def patch_dependencies():
    def render(*_, **__):
        return ""

    with patch("aimet_tensorflow.keras.auto_quant.jinja2.environment.Template.render", side_effect=render):
        yield


class TestAutoQuant:
    @pytest.mark.parametrize(
        "bn_folded_acc, cle_acc, adaround_acc",
        [(50., 60., 70.), (50., 70., 60.), (70., 50., 60.)]
    )
    @pytest.mark.parametrize("allowed_accuracy_drop", [5., 15.])
    def test_auto_quant(self, model, unlabeled_dataset, flag_variables,
                        allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc):
        with patch_ptq_techniques(bn_folded_acc, cle_acc, adaround_acc, flag_variables) as mocks:
            auto_quant = AutoQuant(allowed_accuracy_drop=allowed_accuracy_drop,
                                   eval_callback=mocks.eval_callback,
                                   unlabeled_dataset=unlabeled_dataset)
            self._do_test_apply_auto_quant(auto_quant, model, allowed_accuracy_drop, flag_variables,
                                           bn_folded_acc, cle_acc, adaround_acc)

    @staticmethod
    def _do_test_apply_auto_quant(auto_quant, model, allowed_accuracy_drop,
                                  flag_variables, bn_folded_acc, cle_acc, adaround_acc):
        with create_tmp_directory() as results_dir:
            target_acc = FP32_ACC - allowed_accuracy_drop

            output_model, acc, encoding_path = auto_quant.apply(model, results_dir=results_dir)
            assert_applied_techniques(acc, encoding_path, flag_variables, target_acc, bn_folded_acc)

    def test_auto_quant_invalid_input(self):
        # Allowed accuracy drop < 0
        with pytest.raises(ValueError):
            _ = AutoQuant(-1.0, MagicMock(), MagicMock(), MagicMock())

        # Bitwidth < 4 or bitwidth > 32
        with pytest.raises(ValueError):
            _ = AutoQuant(0, MagicMock(), MagicMock(), default_param_bw=2)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, MagicMock(), MagicMock(), default_param_bw=64)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, MagicMock(), MagicMock(), default_output_bw=2)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, MagicMock(), MagicMock(), default_output_bw=64)

    def test_auto_quant_caching(self, model, unlabeled_dataset, flag_variables):
        allowed_accuracy_drop = 0.0
        bn_folded_acc, cle_acc, adaround_acc = 40., 50., 60.
        with patch_ptq_techniques(bn_folded_acc, cle_acc, adaround_acc, flag_variables) as mocks:
            auto_quant = AutoQuant(allowed_accuracy_drop=allowed_accuracy_drop,
                                   eval_callback=mocks.eval_callback,
                                   unlabeled_dataset=unlabeled_dataset)

            with create_tmp_directory() as results_dir:
                cache_id = "unittest"
                cache_files = [
                    os.path.join(results_dir, ".auto_quant_cache", cache_id, f"{key}.h5")
                    for key in ("batchnorm_folding", "cle", "adaround")
                ]

                # No previously cached results
                auto_quant.apply(model, results_dir=results_dir, cache_id=cache_id)

                # for cache_file in cache_files:
                #     assert os.path.exists(cache_file)

                assert mocks.fold_all_batch_norms.call_count == 1

                # Load cached result
                auto_quant.apply(model, results_dir=results_dir, cache_id=cache_id)

                # PTQ functions should not be called twice.
                # NOTE: Caching feature is not implemented yet, need to modify call_count to 1 after implementation
                assert mocks.fold_all_batch_norms.call_count == 2


@contextlib.contextmanager
def create_tmp_directory(dirname: str = "/tmp/.aimet_unittest"):
    success = False
    try:
        os.makedirs(dirname, exist_ok=True)
        success = True
    except FileExistsError:
        raise

    try:
        yield dirname
    finally:
        if success:
            shutil.rmtree(dirname)