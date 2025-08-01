# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import csv
import json
import logging
import os
import random
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Optional
from unittest import mock

import numpy as np
import torch
from packaging import version

# We use TF to parse the logs
from accelerate import Accelerator
from accelerate.state import PartialState
from accelerate.test_utils.testing import (
    MockingTestCase,
    TempDirTestCase,
    require_aim,
    require_clearml,
    require_comet_ml,
    require_dvclive,
    require_matplotlib,
    require_mlflow,
    require_pandas,
    require_swanlab,
    require_tensorboard,
    require_trackio,
    require_wandb,
    skip,
)
from accelerate.tracking import (
    AimTracker,
    ClearMLTracker,
    CometMLTracker,
    DVCLiveTracker,
    GeneralTracker,
    MLflowTracker,
    SwanLabTracker,
    TensorBoardTracker,
    TrackioTracker,
    WandBTracker,
)
from accelerate.utils import (
    ProjectConfiguration,
    is_comet_ml_available,
    is_dvclive_available,
    is_tensorboard_available,
)


if is_comet_ml_available():
    from comet_ml import ExperimentConfig

if is_tensorboard_available():
    import struct

    import tensorboard.compat.proto.event_pb2 as event_pb2

if is_dvclive_available():
    from dvclive.plots.metric import Metric
    from dvclive.serialize import load_yaml
    from dvclive.utils import parse_metrics

logger = logging.getLogger(__name__)


@require_tensorboard
class TensorBoardTrackingTest(unittest.TestCase):
    @unittest.skipIf(version.parse(np.__version__) >= version.parse("2.0"), "TB doesn't support numpy 2.0")
    def test_init_trackers(self):
        project_name = "test_project_with_config"
        with tempfile.TemporaryDirectory() as dirpath:
            accelerator = Accelerator(log_with="tensorboard", project_dir=dirpath)
            config = {"num_iterations": 12, "learning_rate": 1e-2, "some_boolean": False, "some_string": "some_value"}
            accelerator.init_trackers(project_name, config)
            accelerator.end_training()
            for child in Path(f"{dirpath}/{project_name}").glob("*/**"):
                log = list(filter(lambda x: x.is_file(), child.iterdir()))[0]
            assert str(log) != ""

    def test_log(self):
        project_name = "test_project_with_log"
        with tempfile.TemporaryDirectory() as dirpath:
            accelerator = Accelerator(log_with="tensorboard", project_dir=dirpath)
            accelerator.init_trackers(project_name)
            values = {"total_loss": 0.1, "iteration": 1, "my_text": "some_value"}
            accelerator.log(values, step=0)
            accelerator.end_training()
            # Logged values are stored in the outermost-tfevents file and can be read in as a TFRecord
            # Names are randomly generated each time
            log = list(filter(lambda x: x.is_file(), Path(f"{dirpath}/{project_name}").iterdir()))[0]
            assert str(log) != ""

    def test_log_with_tensor(self):
        project_name = "test_project_with_log"
        with tempfile.TemporaryDirectory() as dirpath:
            accelerator = Accelerator(log_with="tensorboard", project_dir=dirpath)
            accelerator.init_trackers(project_name)
            values = {"tensor": torch.tensor(1)}
            accelerator.log(values, step=0)
            accelerator.end_training()
            # Logged values are stored in the outermost-tfevents file and can be read in as a TFRecord
            # Names are randomly generated each time
            log = list(filter(lambda x: x.is_file(), Path(f"{dirpath}/{project_name}").iterdir()))[0]
            # Reading implementation based on https://github.com/pytorch/pytorch/issues/45327#issuecomment-703757685
            with open(log, "rb") as f:
                data = f.read()
            found_tensor = False
            while data:
                header = struct.unpack("Q", data[:8])

                event_str = data[12 : 12 + int(header[0])]  # 8+4
                data = data[12 + int(header[0]) + 4 :]
                event = event_pb2.Event()

                event.ParseFromString(event_str)
                if event.HasField("summary"):
                    for value in event.summary.value:
                        if value.simple_value == 1.0 and value.tag == "tensor":
                            found_tensor = True
            assert found_tensor, "Converted tensor was not found in the log file!"

    def test_project_dir(self):
        with self.assertRaisesRegex(ValueError, "Logging with `tensorboard` requires a `logging_dir`"):
            _ = Accelerator(log_with="tensorboard")
        with tempfile.TemporaryDirectory() as dirpath:
            _ = Accelerator(log_with="tensorboard", project_dir=dirpath)

    def test_project_dir_with_config(self):
        config = ProjectConfiguration(total_limit=30)
        with tempfile.TemporaryDirectory() as dirpath:
            _ = Accelerator(log_with="tensorboard", project_dir=dirpath, project_config=config)


@require_wandb
@mock.patch.dict(os.environ, {"WANDB_MODE": "offline"})
class WandBTrackingTest(TempDirTestCase, MockingTestCase):
    def setUp(self):
        super().setUp()
        # wandb let's us override where logs are stored to via the WANDB_DIR env var
        self.add_mocks(mock.patch.dict(os.environ, {"WANDB_DIR": self.tmpdir}))

    @staticmethod
    def parse_log(log: str, section: str, record: bool = True):
        """
        Parses wandb log for `section` and returns a dictionary of
        all items in that section. Section names are based on the
        output of `wandb sync --view --verbose` and items starting
        with "Record" in that result
        """
        # Big thanks to the W&B team for helping us parse their logs
        pattern = rf"{section} ([\S\s]*?)\n\n"
        if record:
            pattern = rf"Record: {pattern}"
        cleaned_record = re.findall(pattern, log)[0]
        # A config
        if section == "config" or section == "history":
            cleaned_record = re.findall(r'"([a-zA-Z0-9_.,]+)', cleaned_record)
            return {key: val for key, val in zip(cleaned_record[0::2], cleaned_record[1::2])}
        # Everything else
        else:
            return dict(re.findall(r'(\w+): "([^\s]+)"', cleaned_record))

    @skip
    def test_wandb(self):
        project_name = "test_project_with_config"
        accelerator = Accelerator(log_with="wandb")
        config = {"num_iterations": 12, "learning_rate": 1e-2, "some_boolean": False, "some_string": "some_value"}
        kwargs = {"wandb": {"tags": ["my_tag"]}}
        accelerator.init_trackers(project_name, config, kwargs)
        values = {"total_loss": 0.1, "iteration": 1, "my_text": "some_value"}
        accelerator.log(values, step=0)
        accelerator.end_training()
        # The latest offline log is stored at wandb/latest-run/*.wandb
        for child in Path(f"{self.tmpdir}/wandb/latest-run").glob("*"):
            if child.is_file() and child.suffix == ".wandb":
                cmd = ["wandb", "sync", "--view", "--verbose", str(child)]
                content = subprocess.check_output(cmd, encoding="utf8", errors="ignore")
                break

        # Check HPS through careful parsing and cleaning
        logged_items = self.parse_log(content, "config")
        assert logged_items["num_iterations"] == "12"
        assert logged_items["learning_rate"] == "0.01"
        assert logged_items["some_boolean"] == "false"
        assert logged_items["some_string"] == "some_value"
        assert logged_items["some_string"] == "some_value"

        # Run tags
        logged_items = self.parse_log(content, "run", False)
        assert logged_items["tags"] == "my_tag"

        # Actual logging
        logged_items = self.parse_log(content, "history")
        assert logged_items["total_loss"] == "0.1"
        assert logged_items["iteration"] == "1"
        assert logged_items["my_text"] == "some_value"
        assert logged_items["_step"] == "0"


@require_mlflow
class MLflowTrackingTest(unittest.TestCase):
    def setUp(self):
        import mlflow

        self.tmpdir = tempfile.TemporaryDirectory()
        mlflow.set_tracking_uri("file://" + self.tmpdir.name)

    @require_matplotlib
    def create_mock_figure(self):
        """Create a mock figure for testing."""
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(6, 4))
        return fig

    def test_log(self):
        import mlflow

        """Test that log calls mlflow.log_metrics with only numeric values and the correct step."""
        values = {"accuracy": 0.95, "loss": 0.1, "non_numeric": "ignored"}
        tracker = MLflowTracker(experiment_name="test_exp", logging_dir=self.tmpdir.name)
        accelerator = Accelerator(log_with=tracker)
        accelerator.init_trackers(project_name="test_exp")
        tracker.log(values, step=10)

        run_id = tracker.active_run.info.run_id
        accelerator.end_training()

        # Retrieve the run and check the logged metrics.
        run = mlflow.get_run(run_id)
        metrics = run.data.metrics
        self.assertEqual(metrics.get("accuracy"), 0.95)
        self.assertEqual(metrics.get("loss"), 0.1)
        self.assertNotIn("non_numeric", metrics)

    @require_matplotlib
    def test_log_figure(self):
        import mlflow

        """Test that log_figure calls mlflow.log_figure with the correct arguments."""
        dummy_figure = self.create_mock_figure()
        tracker = MLflowTracker(experiment_name="test_exp", logging_dir=self.tmpdir.name)
        accelerator = Accelerator(log_with=tracker)
        accelerator.init_trackers(project_name="test_exp")
        tracker.log_figure(dummy_figure, artifact_file="dummy_figure.png")

        run_id = tracker.active_run.info.run_id
        accelerator.end_training()

        self.assertIn(
            "dummy_figure.png",
            [artifact.path for artifact in mlflow.artifacts.list_artifacts(run_id=run_id)],
        )

    def test_log_artifact(self):
        import mlflow

        """Test that log_artifact calls mlflow.log_artifact with the correct file path."""
        dummy_file_path = os.path.join(self.tmpdir.name, "dummy.txt")
        with open(dummy_file_path, "w") as f:
            f.write("dummy content")
        tracker = MLflowTracker(experiment_name="test_exp", logging_dir=self.tmpdir.name)
        accelerator = Accelerator(log_with=tracker)
        accelerator.init_trackers(project_name="test_exp")
        tracker.log_artifact(dummy_file_path, artifact_path="artifact_dir")

        run_id = tracker.active_run.info.run_id
        accelerator.end_training()

        self.assertIn(
            "artifact_dir/dummy.txt",
            [
                artifact.path
                for artifact in mlflow.artifacts.list_artifacts(run_id=run_id, artifact_path="artifact_dir")
            ],
        )

    def test_log_artifacts(self):
        import mlflow

        """Test that log_artifacts calls mlflow.log_artifacts with the correct directory."""
        dummy_dir = os.path.join(self.tmpdir.name, "dummy_dir")
        os.mkdir(dummy_dir)
        dummy_file_path = os.path.join(dummy_dir, "dummy.txt")
        with open(dummy_file_path, "w") as f:
            f.write("dummy content")
        tracker = MLflowTracker(experiment_name="test_exp", logging_dir=self.tmpdir.name)
        accelerator = Accelerator(log_with=tracker)
        accelerator.init_trackers(project_name="test_exp")
        tracker.log_artifacts(dummy_dir, artifact_path="artifact_dir")

        run_id = tracker.active_run.info.run_id
        accelerator.end_training()

        self.assertIn(
            "artifact_dir/dummy.txt",
            [
                artifact.path
                for artifact in mlflow.artifacts.list_artifacts(run_id=run_id, artifact_path="artifact_dir")
            ],
        )


@require_comet_ml
class CometMLTest(unittest.TestCase):
    @staticmethod
    def get_value_from_key(log_list, key: str, is_param: bool = False):
        "Extracts `key` from Comet `log`"
        for log in log_list:
            j = json.loads(log)["payload"]
            if is_param and "param" in j.keys():
                if j["param"]["paramName"] == key:
                    return j["param"]["paramValue"]
            if "log_other" in j.keys():
                if j["log_other"]["key"] == key:
                    return j["log_other"]["val"]
            if "metric" in j.keys():
                if j["metric"]["metricName"] == key:
                    return j["metric"]["metricValue"]
            if j.get("key", None) == key:
                return j["value"]

    def test_init_trackers(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = CometMLTracker(
                "test_project_with_config", online=False, experiment_config=ExperimentConfig(offline_directory=d)
            )
            accelerator = Accelerator(log_with=tracker)
            config = {"num_iterations": 12, "learning_rate": 1e-2, "some_boolean": False, "some_string": "some_value"}
            accelerator.init_trackers(None, config)
            accelerator.end_training()
            log = os.listdir(d)[0]  # Comet is nice, it's just a zip file here
            # We parse the raw logs
            p = os.path.join(d, log)
            archive = zipfile.ZipFile(p, "r")
            log = archive.open("messages.json").read().decode("utf-8")
        list_of_json = log.split("\n")[:-1]
        assert self.get_value_from_key(list_of_json, "num_iterations", True) == 12
        assert self.get_value_from_key(list_of_json, "learning_rate", True) == 0.01
        assert self.get_value_from_key(list_of_json, "some_boolean", True) is False
        assert self.get_value_from_key(list_of_json, "some_string", True) == "some_value"

    def test_log(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = CometMLTracker(
                "test_project_with_config", online=False, experiment_config=ExperimentConfig(offline_directory=d)
            )
            accelerator = Accelerator(log_with=tracker)
            accelerator.init_trackers(None)
            values = {"total_loss": 0.1, "iteration": 1, "my_text": "some_value"}
            accelerator.log(values, step=0)
            accelerator.end_training()
            log = os.listdir(d)[0]  # Comet is nice, it's just a zip file here
            # We parse the raw logs
            p = os.path.join(d, log)
            archive = zipfile.ZipFile(p, "r")
            log = archive.open("messages.json").read().decode("utf-8")
        list_of_json = log.split("\n")[:-1]
        assert self.get_value_from_key(list_of_json, "curr_step", True) == 0
        assert self.get_value_from_key(list_of_json, "total_loss") == 0.1
        assert self.get_value_from_key(list_of_json, "iteration") == 1
        assert self.get_value_from_key(list_of_json, "my_text") == "some_value"


@require_clearml
class ClearMLTest(TempDirTestCase, MockingTestCase):
    def setUp(self):
        super().setUp()
        # ClearML offline session location is stored in CLEARML_CACHE_DIR
        self.add_mocks(mock.patch.dict(os.environ, {"CLEARML_CACHE_DIR": str(self.tmpdir)}))

    @staticmethod
    def _get_offline_dir(accelerator):
        from clearml.config import get_offline_dir

        return get_offline_dir(task_id=accelerator.get_tracker("clearml", unwrap=True).id)

    @staticmethod
    def _get_metrics(offline_dir):
        metrics = []
        with open(os.path.join(offline_dir, "metrics.jsonl")) as f:
            json_lines = f.readlines()
            for json_line in json_lines:
                metrics.extend(json.loads(json_line))
        return metrics

    def test_init_trackers(self):
        from clearml import Task
        from clearml.utilities.config import text_to_config_dict

        Task.set_offline(True)
        accelerator = Accelerator(log_with="clearml")
        config = {"num_iterations": 12, "learning_rate": 1e-2, "some_boolean": False, "some_string": "some_value"}
        accelerator.init_trackers("test_project_with_config", config)

        offline_dir = ClearMLTest._get_offline_dir(accelerator)
        accelerator.end_training()

        with open(os.path.join(offline_dir, "task.json")) as f:
            offline_session = json.load(f)
        clearml_offline_config = text_to_config_dict(offline_session["configuration"]["General"]["value"])
        assert config == clearml_offline_config

    def test_log(self):
        from clearml import Task

        Task.set_offline(True)
        accelerator = Accelerator(log_with="clearml")
        accelerator.init_trackers("test_project_with_log")
        values_with_iteration = {"should_be_under_train": 1, "eval_value": 2, "test_value": 3.1, "train_value": 4.1}
        accelerator.log(values_with_iteration, step=1)
        single_values = {"single_value_1": 1.1, "single_value_2": 2.2}
        accelerator.log(single_values)

        offline_dir = ClearMLTest._get_offline_dir(accelerator)
        accelerator.end_training()

        metrics = ClearMLTest._get_metrics(offline_dir)
        assert (len(values_with_iteration) + len(single_values)) == len(metrics)
        for metric in metrics:
            if metric["metric"] == "Summary":
                assert metric["variant"] in single_values
                assert metric["value"] == single_values[metric["variant"]]
            elif metric["metric"] == "should_be_under_train":
                assert metric["variant"] == "train"
                assert metric["iter"] == 1
                assert metric["value"] == values_with_iteration["should_be_under_train"]
            else:
                values_with_iteration_key = metric["variant"] + "_" + metric["metric"]
                assert values_with_iteration_key in values_with_iteration
                assert metric["iter"] == 1
                assert metric["value"] == values_with_iteration[values_with_iteration_key]

    def test_log_images(self):
        from clearml import Task

        Task.set_offline(True)
        accelerator = Accelerator(log_with="clearml")
        accelerator.init_trackers("test_project_with_log_images")

        base_image = np.eye(256, 256, dtype=np.uint8) * 255
        base_image_3d = np.concatenate((np.atleast_3d(base_image), np.zeros((256, 256, 2), dtype=np.uint8)), axis=2)
        images = {
            "base_image": base_image,
            "base_image_3d": base_image_3d,
        }
        accelerator.get_tracker("clearml").log_images(images, step=1)

        offline_dir = ClearMLTest._get_offline_dir(accelerator)
        accelerator.end_training()

        images_saved = Path(os.path.join(offline_dir, "data")).rglob("*.jpeg")
        assert len(list(images_saved)) == len(images)

    def test_log_table(self):
        from clearml import Task

        Task.set_offline(True)
        accelerator = Accelerator(log_with="clearml")
        accelerator.init_trackers("test_project_with_log_table")

        accelerator.get_tracker("clearml").log_table(
            "from lists with columns", columns=["A", "B", "C"], data=[[1, 3, 5], [2, 4, 6]]
        )
        accelerator.get_tracker("clearml").log_table("from lists", data=[["A2", "B2", "C2"], [7, 9, 11], [8, 10, 12]])
        offline_dir = ClearMLTest._get_offline_dir(accelerator)
        accelerator.end_training()

        metrics = ClearMLTest._get_metrics(offline_dir)
        assert len(metrics) == 2
        for metric in metrics:
            assert metric["metric"] in ("from lists", "from lists with columns")
            plot = json.loads(metric["plot_str"])
            if metric["metric"] == "from lists with columns":
                print(plot["data"][0])
                self.assertCountEqual(plot["data"][0]["header"]["values"], ["A", "B", "C"])
                self.assertCountEqual(plot["data"][0]["cells"]["values"], [[1, 2], [3, 4], [5, 6]])
            else:
                self.assertCountEqual(plot["data"][0]["header"]["values"], ["A2", "B2", "C2"])
                self.assertCountEqual(plot["data"][0]["cells"]["values"], [[7, 8], [9, 10], [11, 12]])

    @require_pandas
    def test_log_table_pandas(self):
        import pandas as pd
        from clearml import Task

        Task.set_offline(True)
        accelerator = Accelerator(log_with="clearml")
        accelerator.init_trackers("test_project_with_log_table_pandas")

        accelerator.get_tracker("clearml").log_table(
            "from df", dataframe=pd.DataFrame({"A": [1, 2], "B": [3, 4], "C": [5, 6]}), step=1
        )

        offline_dir = ClearMLTest._get_offline_dir(accelerator)
        accelerator.end_training()

        metrics = ClearMLTest._get_metrics(offline_dir)
        assert len(metrics) == 1
        assert metrics[0]["metric"] == "from df"
        plot = json.loads(metrics[0]["plot_str"])
        self.assertCountEqual(plot["data"][0]["header"]["values"], [["A"], ["B"], ["C"]])
        self.assertCountEqual(plot["data"][0]["cells"]["values"], [[1, 2], [3, 4], [5, 6]])


@require_swanlab
@mock.patch.dict(os.environ, {"SWANLAB_MODE": "offline"})
class SwanLabTrackingTest(TempDirTestCase, MockingTestCase):
    def setUp(self):
        super().setUp()
        # Setting Path where SwanLab parsed log files are saved via the SWANLAB_LOG_DIR env var
        self.add_mocks(mock.patch.dict(os.environ, {"SWANLAB_LOG_DIR": self.tmpdir}))

    @skip
    def test_swanlab(self):
        # Disable hardware monitoring to prevent errors in test mode.
        import swanlab
        from swanlab.log.backup import BackupHandler
        from swanlab.log.backup.datastore import DataStore
        from swanlab.log.backup.models import ModelsParser

        swanlab.merge_settings(swanlab.Settings(hardware_monitor=False))
        # Start a fake training session.
        accelerator = Accelerator(log_with="swanlab")
        project_name = "test_project_with_config"
        experiment_name = "test"
        description = "test project for swanlab"
        tags = ["my_tag"]
        config = {
            "epochs": 10,
            "learning_rate": 0.01,
            "offset": 0.1,
        }
        kwargs = {
            "swanlab": {
                "experiment_name": experiment_name,
                "description": description,
                "tags": tags,
            }
        }
        accelerator.init_trackers(project_name, config, kwargs)
        record_metrics = []
        record_scalars = []
        record_images_count = 0
        record_logs = []
        for epoch in range(1, swanlab.config.epochs):
            acc = 1 - 2**-epoch - random.random() / epoch - 0.1
            loss = 2**-epoch + random.random() / epoch + 0.1
            ll = swanlab.log(
                {
                    "accuracy": acc,
                    "loss": loss,
                    "image": swanlab.Image(np.random.random((3, 3, 3))),
                },
                step=epoch,
            )
            log = f"epoch={epoch}, accuracy={acc}, loss={loss}"
            print(log)
            record_scalars.extend([acc, loss])
            record_images_count += 1
            record_logs.append(log)
            record_metrics.extend([x for _, x in ll.items()])
        accelerator.end_training()

        # Load latest offline log
        run_dir = swanlab.get_run().public.run_dir
        assert os.path.exists(run_dir) is True
        ds = DataStore()
        ds.open_for_scan(os.path.join(run_dir.__str__(), BackupHandler.BACKUP_FILE).__str__())
        with ModelsParser() as models_parser:
            for record in ds:
                if record is None:
                    continue
                models_parser.parse_record(record)
        header, project, experiment, logs, runtime, columns, scalars, medias, footer = models_parser.get_parsed()

        # test file header
        assert header.backup_type == "DEFAULT"

        # test project info
        assert project.name == project_name
        assert project.workspace is None
        assert project.public is None

        # test experiment info
        assert experiment.name is not None
        assert experiment.description == description
        assert experiment.tags == tags

        # test log record
        backup_logs = [log.message for log in logs]
        for record_log in record_logs:
            assert record_log in backup_logs, "Log not found in backup logs: " + record_log

        # test runtime info
        runtime_info = runtime.to_file_model(os.path.join(run_dir.__str__(), "files"))
        assert runtime_info.conda is None, "Not using conda, should be None"
        assert isinstance(runtime_info.requirements, str), "Requirements should be a string"
        assert isinstance(runtime_info.metadata, dict), "Metadata should be a dictionary"
        assert isinstance(runtime_info.config, dict), "Config should be a dictionary"
        for key in runtime_info.config:
            assert key in config, f"Config key {key} not found in original config"
            assert runtime_info.config[key]["value"] == config[key], (
                f"Config value for {key} does not match original value"
            )

        # test scalar
        assert len(scalars) + len(medias) == len(record_metrics), "Total metrics count does not match"
        backup_scalars = [
            metric.metric["data"]
            for metric in record_metrics
            if metric.column_info.chart_type.value.column_type == "FLOAT"
        ]
        assert len(backup_scalars) == len(scalars), "Total scalars count does not match"
        for scalar in backup_scalars:
            assert scalar in record_scalars, f"Scalar {scalar} not found in original scalars"
        backup_images = [
            metric for metric in record_metrics if metric.column_info.chart_type.value.column_type == "IMAGE"
        ]
        assert len(backup_images) == record_images_count, "Total images count does not match"


class MyCustomTracker(GeneralTracker):
    "Basic tracker that writes to a csv for testing"

    _col_names = [
        "total_loss",
        "iteration",
        "my_text",
        "learning_rate",
        "num_iterations",
        "some_boolean",
        "some_string",
    ]

    name = "my_custom_tracker"
    requires_logging_directory = False

    def __init__(self, dir: str, **kwargs):
        super().__init__(**kwargs)
        self.log_dir = dir
        self.f = None
        self.writer = None

    def start(self):
        if self.f is None:
            self.f = open(os.path.join(self.log_dir, "log.csv"), "w+")
            self.writer = csv.DictWriter(self.f, fieldnames=self._col_names)
            self.writer.writeheader()

    @property
    def tracker(self):
        return self.writer

    def store_init_configuration(self, values: dict):
        logger.info("Call init")
        self.writer.writerow(values)

    def log(self, values: dict, step: Optional[int]):
        logger.info("Call log")
        self.writer.writerow(values)

    def finish(self):
        self.f.close()


class CustomTrackerTestCase(unittest.TestCase):
    def test_init_trackers(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = MyCustomTracker(d)
            accelerator = Accelerator(log_with=tracker)
            config = {"num_iterations": 12, "learning_rate": 1e-2, "some_boolean": False, "some_string": "some_value"}
            accelerator.init_trackers("Some name", config)
            accelerator.end_training()
            with open(f"{d}/log.csv") as f:
                data = csv.DictReader(f)
                data = next(data)
                truth = {
                    "total_loss": "",
                    "iteration": "",
                    "my_text": "",
                    "learning_rate": "0.01",
                    "num_iterations": "12",
                    "some_boolean": "False",
                    "some_string": "some_value",
                }
                assert data == truth

    def test_log(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = MyCustomTracker(d)
            accelerator = Accelerator(log_with=tracker)
            accelerator.init_trackers("Some name")
            values = {"total_loss": 0.1, "iteration": 1, "my_text": "some_value"}
            accelerator.log(values, step=0)
            accelerator.end_training()
            with open(f"{d}/log.csv") as f:
                data = csv.DictReader(f)
                data = next(data)
                truth = {
                    "total_loss": "0.1",
                    "iteration": "1",
                    "my_text": "some_value",
                    "learning_rate": "",
                    "num_iterations": "",
                    "some_boolean": "",
                    "some_string": "",
                }
                assert data == truth


@require_dvclive
@mock.patch("dvclive.live.get_dvc_repo", return_value=None)
class DVCLiveTrackingTest(unittest.TestCase):
    def test_init_trackers(self, mock_repo):
        project_name = "test_project_with_config"
        with tempfile.TemporaryDirectory() as dirpath:
            accelerator = Accelerator(log_with="dvclive")
            config = {
                "num_iterations": 12,
                "learning_rate": 1e-2,
                "some_boolean": False,
                "some_string": "some_value",
            }
            init_kwargs = {"dvclive": {"dir": dirpath, "save_dvc_exp": False, "dvcyaml": None}}
            accelerator.init_trackers(project_name, config, init_kwargs)
            accelerator.end_training()
            live = accelerator.trackers[0].live
            params = load_yaml(live.params_file)
            assert params == config

    def test_log(self, mock_repo):
        project_name = "test_project_with_log"
        with tempfile.TemporaryDirectory() as dirpath:
            accelerator = Accelerator(log_with="dvclive", project_dir=dirpath)
            init_kwargs = {"dvclive": {"dir": dirpath, "save_dvc_exp": False, "dvcyaml": None}}
            accelerator.init_trackers(project_name, init_kwargs=init_kwargs)
            values = {"total_loss": 0.1, "iteration": 1, "my_text": "some_value"}
            # Log step 0
            accelerator.log(values)
            # Log step 1
            accelerator.log(values)
            # Log step 3 (skip step 2)
            accelerator.log(values, step=3)
            accelerator.end_training()
            live = accelerator.trackers[0].live
            logs, latest = parse_metrics(live)
            assert latest.pop("step") == 3
            assert latest == values
            scalars = os.path.join(live.plots_dir, Metric.subfolder)
            for val in values.keys():
                val_path = os.path.join(scalars, f"{val}.tsv")
                steps = [int(row["step"]) for row in logs[val_path]]
                assert steps == [0, 1, 3]


class TrackerDeferredInitializationTest(unittest.TestCase):
    """
    Tests tracker's deferred initialization via `start()` method, preventing
    premature `PartialState` access (and `torch.distributed` init) before
    `Accelerator` has configured the distributed environment, especially with
    `InitProcessGroupKwargs`.
    """

    @require_tensorboard
    def test_tensorboard_deferred_init(self):
        """Test that TensorBoard tracker initialization doesn't initialize distributed"""
        with tempfile.TemporaryDirectory() as temp_dir:
            PartialState._reset_state()
            tracker = TensorBoardTracker(run_name="test_tb", logging_dir=temp_dir)
            self.assertEqual(PartialState._shared_state, {})
            _ = Accelerator(log_with=tracker)
            self.assertNotEqual(PartialState._shared_state, {})

    @require_wandb
    def test_wandb_deferred_init(self):
        """Test that WandB tracker initialization doesn't initialize distributed"""
        PartialState._reset_state()
        tracker = WandBTracker(run_name="test_wandb")
        self.assertEqual(PartialState._shared_state, {})
        _ = Accelerator(log_with=tracker)
        self.assertNotEqual(PartialState._shared_state, {})

    @require_trackio
    def test_trackio_deferred_init(self):
        """Test that trackio tracker initialization doesn't initialize distributed"""
        PartialState._reset_state()
        tracker = TrackioTracker(run_name="test_trackio")
        self.assertEqual(PartialState._shared_state, {})
        _ = Accelerator(log_with=tracker)
        self.assertNotEqual(PartialState._shared_state, {})

    @require_comet_ml
    def test_comet_ml_deferred_init(self):
        """Test that CometML tracker initialization doesn't initialize distributed"""
        PartialState._reset_state()
        tracker = CometMLTracker(run_name="test_comet")
        self.assertEqual(PartialState._shared_state, {})
        _ = Accelerator(log_with=tracker)
        self.assertNotEqual(PartialState._shared_state, {})

    @require_aim
    def test_aim_deferred_init(self):
        """Test that Aim tracker initialization doesn't initialize distributed"""
        with tempfile.TemporaryDirectory() as temp_dir:
            PartialState._reset_state()
            tracker = AimTracker(run_name="test_aim", repo=temp_dir)
            self.assertEqual(PartialState._shared_state, {})
            _ = Accelerator(log_with=tracker)
            self.assertNotEqual(PartialState._shared_state, {})

    @require_mlflow
    def test_mlflow_deferred_init(self):
        """Test that MLflow tracker initialization doesn't initialize distributed"""
        with tempfile.TemporaryDirectory() as temp_dir:
            PartialState._reset_state()
            tracker = MLflowTracker(experiment_name="test_mlflow", logging_dir=temp_dir)
            self.assertEqual(PartialState._shared_state, {})
            _ = Accelerator(log_with=tracker)
            self.assertNotEqual(PartialState._shared_state, {})

    @require_clearml
    def test_clearml_deferred_init(self):
        """Test that ClearML tracker initialization doesn't initialize distributed"""
        PartialState._reset_state()
        tracker = ClearMLTracker(run_name="test_clearml")
        self.assertEqual(PartialState._shared_state, {})
        _ = Accelerator(log_with=tracker)
        self.assertNotEqual(PartialState._shared_state, {})

    @require_dvclive
    def test_dvclive_deferred_init(self):
        """Test that DVCLive tracker initialization doesn't initialize distributed"""
        with tempfile.TemporaryDirectory() as temp_dir:
            PartialState._reset_state()
            tracker = DVCLiveTracker(dir=temp_dir)
            self.assertEqual(PartialState._shared_state, {})
            _ = Accelerator(log_with=tracker)
            self.assertNotEqual(PartialState._shared_state, {})

    @require_swanlab
    def test_swanlab_deferred_init(self):
        """Test that SwanLab tracker initialization doesn't initialize distributed"""
        PartialState._reset_state()
        tracker = SwanLabTracker(run_name="test_swanlab")
        self.assertEqual(PartialState._shared_state, {})
        _ = Accelerator(log_with=tracker)
        self.assertNotEqual(PartialState._shared_state, {})
