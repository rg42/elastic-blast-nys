#                           PUBLIC DOMAIN NOTICE
#              National Center for Biotechnology Information
#  
# This software is a "United States Government Work" under the
# terms of the United States Copyright Act.  It was written as part of
# the authors' official duties as United States Government employees and
# thus cannot be copyrighted.  This software is freely available
# to the public for use.  The National Library of Medicine and the U.S.
# Government have not placed any restriction on its use or reproduction.
#   
# Although all reasonable efforts have been taken to ensure the accuracy
# and reliability of the software and data, the NLM and the U.S.
# Government do not and cannot warrant the performance or results that
# may be obtained by using this software or data.  The NLM and the U.S.
# Government disclaim all warranties, express or implied, including
# warranties of performance, merchantability or fitness for any particular
# purpose.
#   
# Please cite NCBI in any work or product based on this material.

"""
Unit tests for elastic-blast submit task

Author: Greg Boratyn boratyng@ncbi.nlm.nih.gov
"""

from argparse import Namespace
import os
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from elastic_blast.commands.submit import submit, assemble_query_file_list
from elastic_blast.commands.submit import are_files_on_localhost
from elastic_blast.util import UserReportError
from elastic_blast import constants
from elastic_blast import gcp
from elastic_blast.config import configure
from elastic_blast.constants import QUERY_LIST_EXT, ElbCommand
from elastic_blast.elb_config import ElasticBlastConfig
from elastic_blast.base import InstanceProperties

from tests.utils import gke_mock, MockedCompletedProcess
import pytest

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
INI_NO_BLASTDB = os.path.join(DATA_DIR, 'blastdb-notfound.ini')


def test_to_determine_whether_queries_are_local():
    assert are_files_on_localhost([INI_NO_BLASTDB])
    assert not are_files_on_localhost(['s3://foo-bar', INI_NO_BLASTDB])
    assert not are_files_on_localhost([INI_NO_BLASTDB, 'gs://foo-bar'])
    assert not are_files_on_localhost(['http://foo-bar'])
    assert not are_files_on_localhost(['https://foo-bar'])
    assert not are_files_on_localhost(['ftp://foo-bar'])
    assert are_files_on_localhost(['this-file-does-not-exist'])
    assert are_files_on_localhost(['/tmp/this-file-does-not-existsftp'])
    assert are_files_on_localhost(['/tmp/this-file-does-not-existss3://'])

## Mocked tests

def test_blastdb_not_found(gke_mock, mocker):
    """Test that UserReportError is raised when database is not found"""
    def mocked_check_cluster(cfg):
        """Mocked check cluster that simulates non-existent cluster status"""
        return ''
    mocker.patch('elastic_blast.commands.submit.gcp_check_cluster', side_effect=mocked_check_cluster)
    def mock_safe_exec(cmd):
        if isinstance(cmd, list):
            cmd = ' '.join(cmd)
        if cmd == 'gsutil cat gs://blast-db/latest-dir':
            return MockedCompletedProcess(stdout='2020-20-20')
        elif cmd == 'gsutil cat gs://blast-db/2020-20-20/blastdb-manifest.json':
            return MockedCompletedProcess(stdout='{"nt":{"size":93.36}, "nr":{"size":227.4}}')
        return MockedCompletedProcess()
    mocker.patch('elastic_blast.util.safe_exec', side_effect=mock_safe_exec)

    print(INI_NO_BLASTDB)

    args = Namespace(cfg=INI_NO_BLASTDB)

    # test that UserReportError is raised
    with pytest.raises(UserReportError) as err:
        submit(args, ElasticBlastConfig(configure(args), task = ElbCommand.SUBMIT), [])

    # test error code and message
    assert err.value.returncode == constants.BLASTDB_ERROR
    assert 'BLAST database' in err.value.message
    assert 'not found' in err.value.message


@pytest.fixture
def tmpdir():
    """Fixture that creates a temporary directory and deletes it after a test"""
    name = tempfile.mkdtemp()
    yield name
    shutil.rmtree(name)


@patch(target='elastic_blast.elb_config.aws_get_machine_properties', new=MagicMock(return_value=InstanceProperties(32, 120)))
def test_query_file_list(tmpdir):
    """Test getting a list of queries files from a file"""
    expected_query_files = ['query-file-1', 'query-file-2']
    query_list_file = os.path.join(tmpdir, 'queries' + QUERY_LIST_EXT)
    with open(query_list_file, 'w') as f:
        for item in expected_query_files:
            f.write(item)
            f.write('\n')
        f.flush()

    cfg = ElasticBlastConfig(aws_region = 'test-region',
                             program = 'blastn',
                             db = 'test-db',
                             queries = query_list_file,
                             results = 's3://test-results',
                             task = ElbCommand.SUBMIT)

    query_files = assemble_query_file_list(cfg)
    assert sorted(query_files) == sorted(expected_query_files)


@patch(target='elastic_blast.elb_config.aws_get_machine_properties', new=MagicMock(return_value=InstanceProperties(32, 120)))
def test_query_file_list_bad_uri(tmpdir):
    """Test list of queries with illigal cloud URIs"""
    query_files = ['gs://bucket!-123/@#$*/queris!.fa',
                   's3://bucket!-123/@#$*/queris!.fa']
    query_list_file = os.path.join(tmpdir, 'queries' + QUERY_LIST_EXT)
    with open(query_list_file, 'w') as f:
        for item in query_files:
            f.write(item)
            f.write('\n')
        f.flush()

    cfg = ElasticBlastConfig(aws_region = 'test-region',
                             program = 'blastn',
                             db = 'test-db',
                             queries = query_list_file,
                             results = 's3://test-results',
                             task = ElbCommand.SUBMIT)

    with pytest.raises(UserReportError) as err:
        assemble_query_file_list(cfg)
    assert 'Incorrect query' in err.value.message
    for query in query_files:
        assert query in err.value.message



## Real tests that may create cloud resources

# A few test require specific GCP credentials and may create GCP resources.
# They are skipped. Set environment variable RUN_ALL_TESTS to run all tests.
SKIP = not os.getenv('RUN_ALL_TESTS')

@pytest.fixture
def blastdb_not_found_fixture():
    """Cleanup cluster if it was created"""
    # setup
    args = Namespace(cfg=INI_NO_BLASTDB)
    yield args

    # teardown
    cfg = configparser.ConfigParser()
    cfg.read(args.cfg)
    cfg = ElasticBlastConfig(cfg, task = ElbCommand.SUBMIT)
    gcp.delete_cluster_with_cleanup(cfg)


@pytest.mark.skipif(SKIP, reason='This test requires specific GCP credentials and may create GCP resources. It should be used with care.')
def test_blastdb_not_found_real(blastdb_not_found_fixture):
    """Test that UserReportError is raised when database is not found"""
    args = blastdb_not_found_fixture
    
    # test that UserReportError is raised
    with pytest.raises(UserReportError) as err:
        submit(args, [])

    # test error code and message
    assert err.value.returncode == constants.BLASTDB_ERROR
    assert 'BLAST database' in err.value.message
    assert 'not found' in err.value.message
