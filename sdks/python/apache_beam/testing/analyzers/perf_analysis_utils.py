#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import abc
import logging
from dataclasses import asdict
from dataclasses import dataclass
from statistics import median
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import pandas as pd
import yaml
from google.api_core import exceptions
from google.cloud import bigquery

from apache_beam.testing.analyzers import constants
from apache_beam.testing.analyzers import github_issues_utils
from apache_beam.testing.load_tests import load_test_metrics_utils
from apache_beam.testing.load_tests.load_test_metrics_utils import BigQueryMetricsPublisher
from signal_processing_algorithms.energy_statistics.energy_statistics import e_divisive


@dataclass(frozen=True)
class GitHubIssueMetaData:
  """
  This class holds metadata that needs to be published to the
  BigQuery when a GitHub issue is created on a performance
  alert.
  """
  issue_timestamp: pd.Timestamp
  change_point_timestamp: pd.Timestamp
  test_name: str
  metric_name: str
  issue_number: int
  issue_url: str
  test_id: str
  change_point: float


def is_change_point_in_valid_window(
    num_runs_in_change_point_window: int, latest_change_point_run: int) -> bool:
  return num_runs_in_change_point_window > latest_change_point_run


def get_existing_issues_data(table_name: str) -> Optional[pd.DataFrame]:
  """
  Finds the most recent GitHub issue created for the test_name.
  If no table found with name=test_name, return (None, None)
  else return latest created issue_number along with
  """
  query = f"""
  SELECT * FROM {constants._BQ_PROJECT_NAME}.{constants._BQ_DATASET}.{table_name}
  ORDER BY {constants._ISSUE_CREATION_TIMESTAMP_LABEL} DESC
  LIMIT 10
  """
  try:
    client = bigquery.Client()
    query_job = client.query(query=query)
    existing_issue_data = query_job.result().to_dataframe()
  except exceptions.NotFound:
    # If no table found, that means this is first performance regression
    # on the current test+metric.
    return None
  return existing_issue_data


def is_perf_alert(
    previous_change_point_timestamps: List[pd.Timestamp],
    change_point_index: int,
    timestamps: List[pd.Timestamp],
    min_runs_between_change_points: int) -> bool:
  """
  Search the previous_change_point_timestamps with current observed
  change point sibling window and determine if it is a duplicate
  change point or not.
  timestamps are expected to be in ascending order.

  Return False if the current observed change point is a duplicate of
  already reported change points else return True.
  """
  sibling_change_point_min_timestamp = timestamps[max(
      0, change_point_index - min_runs_between_change_points)]
  sibling_change_point_max_timestamp = timestamps[min(
      change_point_index + min_runs_between_change_points, len(timestamps) - 1)]
  # Search a list of previous change point timestamps and compare it with
  # current change point timestamp. We do this in case, if a current change
  # point is already reported in the past.
  for previous_change_point_timestamp in previous_change_point_timestamps:
    if (sibling_change_point_min_timestamp <= previous_change_point_timestamp <=
        sibling_change_point_max_timestamp):
      return False
  return True


def read_test_config(config_file_path: str) -> Dict:
  """
  Reads the config file in which the data required to
  run the change point analysis is specified.
  """
  with open(config_file_path, 'r') as stream:
    config = yaml.safe_load(stream)
  return config


def validate_config(keys):
  return constants._PERF_TEST_KEYS.issubset(keys)


def find_change_points(metric_values: List[Union[float, int]]):
  return e_divisive(metric_values)


def find_latest_change_point_index(metric_values: List[Union[float, int]]):
  """
  Args:
   metric_values: Metric values used to run change point analysis.
  Returns:
   int: Right most change point index observed on metric_values.
  """
  change_points_indices = find_change_points(metric_values)
  # reduce noise in the change point analysis by filtering out
  # the change points that are not significant enough.
  change_points_indices = filter_change_points_by_median_threshold(
      metric_values, change_points_indices)
  # Consider the latest change point.
  if not change_points_indices:
    return None
  change_points_indices.sort()
  return change_points_indices[-1]


def publish_issue_metadata_to_big_query(issue_metadata, table_name):
  """
  Published issue_metadata to BigQuery with table name.
  """
  bq_metrics_publisher = BigQueryMetricsPublisher(
      project_name=constants._BQ_PROJECT_NAME,
      dataset=constants._BQ_DATASET,
      table=table_name,
      bq_schema=constants._SCHEMA)
  bq_metrics_publisher.publish([asdict(issue_metadata)])
  logging.info(
      'GitHub metadata is published to Big Query Dataset %s'
      ', table %s' % (constants._BQ_DATASET, table_name))


def create_performance_alert(
    metric_name: str,
    test_id: str,
    timestamps: List[pd.Timestamp],
    metric_values: List[Union[int, float]],
    change_point_index: int,
    labels: List[str],
    existing_issue_number: Optional[int],
    test_description: Optional[str] = None,
    test_name: Optional[str] = None,
) -> Tuple[int, str]:
  """
  Creates performance alert on GitHub issues and returns GitHub issue
  number and issue URL.
  """
  description = github_issues_utils.get_issue_description(
      test_id=test_id,
      test_name=test_name,
      test_description=test_description,
      metric_name=metric_name,
      timestamps=timestamps,
      metric_values=metric_values,
      change_point_index=change_point_index,
      max_results_to_display=(
          constants._NUM_RESULTS_TO_DISPLAY_ON_ISSUE_DESCRIPTION))

  issue_number, issue_url = github_issues_utils.report_change_point_on_issues(
        title=github_issues_utils._ISSUE_TITLE_TEMPLATE.format(
          test_id, metric_name
        ),
        description=description,
        labels=labels,
        existing_issue_number=existing_issue_number)

  logging.info(
      'Performance regression/improvement is alerted on issue #%s. Link '
      ': %s' % (issue_number, issue_url))
  return issue_number, issue_url


def filter_change_points_by_median_threshold(
    data: List[Union[int, float]],
    change_points: List[int],
    threshold: float = 0.05,
):
  """
  Reduces the number of change points by filtering out the ones that are
  not significant enough based on the relative median threshold. Default
  value of threshold is 0.05.
  """
  valid_change_points = []
  epsilon = 1e-10  # needed to avoid division by zero.

  for idx in change_points:
    if idx == 0 or idx == len(data):
      continue

    left_segment = data[:idx]
    right_segment = data[idx:]

    left_value = median(left_segment)
    right_value = median(right_segment)

    relative_change = abs(right_value - left_value) / (left_value + epsilon)

    if relative_change > threshold:
      valid_change_points.append(idx)
  return valid_change_points


class MetricsFetcher(metaclass=abc.ABCMeta):
  @abc.abstractmethod
  def fetch_metric_data(
      self,
      *,
      project,
      metrics_dataset,
      metrics_table,
      metric_name,
      test_name=None) -> Tuple[List[Union[int, float]], List[pd.Timestamp]]:
    """
    Define SQL query and fetch the timestamp values and metric values
    from BigQuery tables.
    """
    raise NotImplementedError


class BigQueryMetricsFetcher(MetricsFetcher):
  def fetch_metric_data(
      self,
      *,
      project,
      metrics_dataset,
      metrics_table,
      metric_name,
      test_name=None,
  ) -> Tuple[List[Union[int, float]], List[pd.Timestamp]]:
    """
    Args:
    params: Dict containing keys required to fetch data from a data source.
    Returns:
      Tuple[List[Union[int, float]], List[pd.Timestamp]]: Tuple containing list
      of metric_values and list of timestamps. Both are sorted in ascending
      order wrt timestamps.
    """
    query = f"""
          SELECT *
          FROM {project}.{metrics_dataset}.{metrics_table}
          WHERE CONTAINS_SUBSTR(({load_test_metrics_utils.METRICS_TYPE_LABEL}), '{metric_name}')
          ORDER BY {load_test_metrics_utils.SUBMIT_TIMESTAMP_LABEL} DESC
          LIMIT {constants._NUM_DATA_POINTS_TO_RUN_CHANGE_POINT_ANALYSIS}
        """
    client = bigquery.Client()
    query_job = client.query(query=query)
    metric_data = query_job.result().to_dataframe()
    metric_data.sort_values(
        by=[load_test_metrics_utils.SUBMIT_TIMESTAMP_LABEL], inplace=True)
    return (
        metric_data[load_test_metrics_utils.VALUE_LABEL].tolist(),
        metric_data[load_test_metrics_utils.SUBMIT_TIMESTAMP_LABEL].tolist())
