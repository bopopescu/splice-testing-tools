import splicetestlib
from splicetestlib.splice_testcase import *
import nose

class test_splice_bug972953(SpliceTestcase, Splice_has_FAKE_SPACEWALK):
    ''' Depens on test_bug972942 data '''
    def _setup(self):
        splicetestlib.cleanup_katello(self.ss.Instances["KATELLO"][0])
        splicetestlib.fake_spacewalk_test(self.ss.Instances["FAKE_SPACEWALK"][0], "test_bug972942")
        splicetestlib.sst_step(self.ss.Instances["FAKE_SPACEWALK"][0])

    def _test(self):
        nose.tools.assert_true('Org Admin Role for TestOrg1' in [role['name'] for role in self.katello.list_roles()])
        splicetestlib.sst_step(self.ss.Instances["FAKE_SPACEWALK"][0])
        nose.tools.assert_true('Org Admin Role for TestOrg2' in [role['name'] for role in self.katello.list_roles()])

    def _cleanup(self):
        pass

if __name__ == "__main__":
    nose.run(defaultTest=__name__, argv=[__file__, '-v'])
