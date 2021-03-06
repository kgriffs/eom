# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR ONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import logging
import multiprocessing
import sys
import time
from wsgiref import simple_server

import eom.governor
import requests

from tests import util


class TestGovernor(util.TestCase):

    def setUp(self):
        super(TestGovernor, self).setUp()

        self.governor = eom.governor.wrap(util.app)

        config = eom.governor.CONF['eom:governor']
        self.node_count = config['node_count']
        self.period_sec = config['period_sec']
        rates = eom.governor._load_rates(config['rates_file'],
                                         self.period_sec, self.node_count)

        self.test_rate = rates[0]
        self.soft_limit = self.test_rate.soft_limit
        self.hard_limit = self.test_rate.hard_limit
        self.test_url = '/v1/queues/fizbit/messages'

        self.default_rate = rates[1]

    def _quantum_leap(self):
        # Wait until the next time quantum
        normalized = time.time() % (self.period_sec * 2)
        if normalized < self.period_sec:
            time.sleep(self.period_sec - normalized)
        else:
            time.sleep(self.period_sec * 2 - normalized)

    def test_missing_project_id(self):
        env = self.create_env('/v1')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '400 Bad Request')

    def test_simple(self):
        env = self.create_env('/v1', project_id='84197')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '204 No Content')

    def test_soft_limit(self):
        self._test_limit(self.soft_limit, 204)

    def test_soft_limit_burst(self):
        self._test_limit(self.soft_limit, 204, burst=True)

    def test_soft_limit_default(self):
        self._test_limit(self.default_rate.soft_limit, 204, 'PATCH')

    def test_hard_limit(self):
        self._test_limit(self.hard_limit, 429)

    def test_hard_limit_burst(self):
        self._test_limit(self.hard_limit, 429, burst=True)

    #----------------------------------------------------------------------
    # Helpers
    #----------------------------------------------------------------------

    def _test_limit(self, limit, expected_status,
                    http_method='GET', burst=False):

        request = getattr(requests, http_method.lower())

        def run_server():
            sys.stderr = io.BytesIO()  # Suppress logging

            httpd = simple_server.make_server('127.0.0.1', 8783,
                                              self.governor)
            httpd.serve_forever()

        process = multiprocessing.Process(target=run_server)
        process.daemon = True
        process.start()

        # Give the process a moment to start up
        time.sleep(0.1)

        num_periods = 5
        sec_per_req = float(self.period_sec) / limit
        url = 'http://127.0.0.1:8783' + self.test_url

        # Suppress logging
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        # Start out at the beginning of a time bucket
        self._quantum_leap()

        if burst:
            for i in range(limit + limit / 2):
                request(url, headers={'X-Project-ID': 1234})

            self._quantum_leap()

        start = time.time()
        stop_1 = start + self.period_sec
        stop_N = start + self.period_sec * num_periods

        # Slightly exceed the limit
        sleep_per_req = 0.7 * sec_per_req
        batch_size = int(0.1 / sleep_per_req)
        sleep_per_batch = batch_size * sleep_per_req

        num_requests = 0
        while time.time() < stop_1:
            resp = request(url, headers={'X-Project-ID': 1234})

            # Only sleep every N requests
            num_requests += 1
            if num_requests % batch_size == 0:
                time.sleep(sleep_per_batch)

        num_requests = 0
        while time.time() < stop_N:
            resp = request(url, headers={'X-Project-ID': 1234})
            self.assertEquals(resp.status_code, expected_status)

            num_requests += 1

            # Only sleep every N requests
            if num_requests % batch_size == 0:
                time.sleep(sleep_per_batch)

        if expected_status == 204:
            # We would have slept so we can predict
            # the rate.
            expected = limit * (num_periods - 1)

            # Expect that we allowed a slightly faster rate per the
            # sleep_offset setting.
            self.assertGreater(num_requests, expected)
            self.assertAlmostEqual(num_requests, expected,
                                   delta=(150 / self.node_count))

        process.terminate()
