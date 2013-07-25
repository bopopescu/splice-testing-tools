class SpliceTestFailed(AssertionError):
    '''
    Splice testing exception
    '''
    pass

def _run_command(connection, command, timeout=60):
    """ Run a command and raise exception in case of timeout or none-zero result """
    status = connection.recv_exit_status(command, timeout, get_pty=True)
    if status is not None and status != 0:
        raise SpliceTestFailed("Failed to run %s: got %s return value\nSTDOUT: %s\nSTDERR: %s" % (command, status, connection.last_stdout, connection.last_stderr))
    elif status is None:
        raise SpliceTestFailed("Failed to run %s: got timeout %s\nSTDOUT: %s\nSTDERR: %s" % (command, timeout, connection.last_stdout, connection.last_stderr))

def run_sst(connection, spacewalk_only=False, splice_only=False, timeout=120):
    """ Run spacewalk-splice-tool """
    command = "sudo -u splice spacewalk-splice-checkin"
    if spacewalk_only:
        command += " --spacewalk-sync"
    elif splice_only:
        command += " --splice-sync"
    _run_command(connection, command, timeout)

def fake_spacewalk_env(connection, test_name):
    """ Select test with fake spacewalk """
    _run_command(connection, "spacewalk-report-set %s" % test_name)

def fake_spacewalk_step(connection):
    """ Step to next data """
    _run_command(connection, "spacewalk-report-set -n")

def sst_step(connection, fake_spacewalk_connection=None, timeout=120):
    """ Run sst and step to next data (iteration) """
    run_sst(connection, timeout=timeout)
    if fake_spacewalk_connection is None:
        fake_spacewalk_step(connection)
    else:
        fake_spacewalk_step(fake_spacewalk_connection)

def cleanup_katello(connection, keep_splice=False, password='admin'):
    """ Clean up katello and splice databases """
    _run_command(connection, "katello-configure --user-pass='%s' --reset-data=YES" % password, 500)
    if not keep_splice:
        _run_command(connection, "service splice_all stop ||:")
        _run_command(connection, "mongo checkin_service --eval 'db.dropDatabase()'")
        _run_command(connection, "service splice_all start ||:")

def parse_report_json(report):
    """ Read json report and figure out important facts """
    result = {}
    result["total_number_of_instances"] = len(report)
    result["number_of_current"] = sum(1 for instance in report if instance['entitlement_status']['status'] == 'valid')
    result["number_of_insufficient"] = sum(1 for instance in report if instance['entitlement_status']['status'] == 'parital')
    result["number_of_invalid"] = sum(1 for instance in report if instance['entitlement_status']['status'] == 'invalid')
    return result
