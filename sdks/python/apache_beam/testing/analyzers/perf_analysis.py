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

# This script is used to run Change Point Analysis using a config file.
# config file holds the parameters required to fetch data, and to run the
# change point analysis. Change Point Analysis is used to find Performance
# regressions for benchmark/load/performance test.

import argparse
import logging
import os
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional

import pandas as pd

from apache_beam.testing.analyzers import constants
from apache_beam.testing.analyzers.perf_analysis_utils import BigQueryMetricsFetcher
from apache_beam.testing.analyzers.perf_analysis_utils import GitHubIssueMetaData
from apache_beam.testing.analyzers.perf_analysis_utils import MetricsFetcher
from apache_beam.testing.analyzers.perf_analysis_utils import create_performance_alert
from apache_beam.testing.analyzers.perf_analysis_utils import find_latest_change_point_index
from apache_beam.testing.analyzers.perf_analysis_utils import get_existing_issues_data
from apache_beam.testing.analyzers.perf_analysis_utils import is_change_point_in_valid_window
from apache_beam.testing.analyzers.perf_analysis_utils import is_perf_alert
from apache_beam.testing.analyzers.perf_analysis_utils import publish_issue_metadata_to_big_query
from apache_beam.testing.analyzers.perf_analysis_utils import read_test_config
from apache_beam.testing.analyzers.perf_analysis_utils import validate_config


def run_change_point_analysis(
    params, test_id, big_query_metrics_fetcher: MetricsFetcher):
  """
  Args:
   params: Dict containing parameters to run change point analysis.
   test_id: Test id for the current test.
   big_query_metrics_fetcher: BigQuery metrics fetcher used to fetch data for
    change point analysis.
  Returns:
     bool indicating if a change point is observed and alerted on GitHub.
  """
  logging.info("Running change point analysis for test ID %s" % test_id)
  if not validate_config(params.keys()):
    raise ValueError(
        f"Please make sure all these keys {constants._PERF_TEST_KEYS} "
        f"are specified for the {test_id}")

  metric_name = params['metric_name']

  # test_name will be used to query a single test from
  # multiple tests in a single BQ table. Right now, the default
  # assumption is that all the test have an individual BQ table
  # but this might not be case for other tests(such as IO tests where
  # a single BQ tables stores all the data)
  test_name = params.get('test_name', None)

  min_runs_between_change_points = (
      constants._DEFAULT_MIN_RUNS_BETWEEN_CHANGE_POINTS)
  if 'min_runs_between_change_points' in params:
    min_runs_between_change_points = params['min_runs_between_change_points']

  num_runs_in_change_point_window = (
      constants._DEFAULT_NUM_RUMS_IN_CHANGE_POINT_WINDOW)
  if 'num_runs_in_change_point_window' in params:
    num_runs_in_change_point_window = params['num_runs_in_change_point_window']

  metric_values, timestamps = big_query_metrics_fetcher.fetch_metric_data(
    project=params['project'],
    metrics_dataset=params['metrics_dataset'],
    metrics_table=params['metrics_table'],
    metric_name=params['metric_name'],
    test_name=test_name
  )

  change_point_index = find_latest_change_point_index(
      metric_values=metric_values)
  if not change_point_index:
    logging.info("Change point is not detected for the test ID %s" % test_id)
    return False
  # since timestamps are ordered in ascending order and
  # num_runs_in_change_point_window refers to the latest runs,
  # latest_change_point_run can help determine if the change point
  # index is recent wrt num_runs_in_change_point_window
  latest_change_point_run = len(timestamps) - 1 - change_point_index
  if not is_change_point_in_valid_window(num_runs_in_change_point_window,
                                         latest_change_point_run):
    logging.info(
        'Performance regression/improvement found for the test ID: %s. '
        'on metric %s. Since the change point run %s '
        'lies outside the num_runs_in_change_point_window distance: %s, '
        'alert is not raised.' % (
            test_id,
            metric_name,
            latest_change_point_run + 1,
            num_runs_in_change_point_window))
    return False

  is_alert = True
  last_reported_issue_number = None
  issue_metadata_table_name = f'{params.get("metrics_table")}_{metric_name}'
  existing_issue_data = get_existing_issues_data(
      table_name=issue_metadata_table_name)

  if existing_issue_data is not None:
    existing_issue_timestamps = existing_issue_data[
        constants._CHANGE_POINT_TIMESTAMP_LABEL].tolist()
    last_reported_issue_number = existing_issue_data[
        constants._ISSUE_NUMBER].tolist()[0]
    # convert numpy.int64 to int
    last_reported_issue_number = last_reported_issue_number.item()

    is_alert = is_perf_alert(
        previous_change_point_timestamps=existing_issue_timestamps,
        change_point_index=change_point_index,
        timestamps=timestamps,
        min_runs_between_change_points=min_runs_between_change_points)
  if is_alert:
    issue_number, issue_url = create_performance_alert(
    metric_name, test_id, timestamps,
    metric_values, change_point_index,
    params.get('labels', None),
    last_reported_issue_number,
    test_description = params.get('test_description', None),
    test_name = test_name
    )

    issue_metadata = GitHubIssueMetaData(
        issue_timestamp=pd.Timestamp(
            datetime.now().replace(tzinfo=timezone.utc)),
        # BQ doesn't allow '.' in table name
        test_id=test_id.replace('.', '_'),
        test_name=test_name,
        metric_name=metric_name,
        change_point=metric_values[change_point_index],
        issue_number=issue_number,
        issue_url=issue_url,
        change_point_timestamp=timestamps[change_point_index])

    publish_issue_metadata_to_big_query(
        issue_metadata=issue_metadata, table_name=issue_metadata_table_name)

  return is_alert


def run(
    *,
    big_query_metrics_fetcher: MetricsFetcher = BigQueryMetricsFetcher(),
    config_file_path: Optional[str] = None,
) -> None:
  """
  run is the entry point to run change point analysis on test metric
  data, which is read from config file, and if there is a performance
  regression/improvement observed for a test, an alert
  will filed with GitHub Issues.

  If config_file_path is None, then the run method will use default
  config file to read the required perf test parameters.

  Please take a look at the README for more information on the parameters
  defined in the config file.

  """
  if config_file_path is None:
    config_file_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'tests_config.yaml')

  tests_config: Dict[str, Dict[str, Any]] = read_test_config(config_file_path)

  for test_id, params in tests_config.items():
    run_change_point_analysis(
        params=params,
        test_id=test_id,
        big_query_metrics_fetcher=big_query_metrics_fetcher)


if __name__ == '__main__':
  logging.basicConfig(level=logging.INFO)

  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--config_file_path',
      default=None,
      type=str,
      help='Path to the config file that contains data to run the Change Point '
      'Analysis.The default file will used will be '
      'apache_beam/testing/analyzers/tests.config.yml. '
      'If you would like to use the Change Point Analysis for finding '
      'performance regression in the tests, '
      'please provide an .yml file in the same structure as the above '
      'mentioned file. ')
  known_args, unknown_args = parser.parse_known_args()

  if unknown_args:
    logging.warning('Discarding unknown arguments : %s ' % unknown_args)

  run(config_file_path=known_args.config_file_path)
