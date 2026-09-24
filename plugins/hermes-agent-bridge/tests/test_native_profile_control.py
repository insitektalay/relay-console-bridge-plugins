import sys,tempfile,types,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from native_profile_control import apply
class NativeProfile(unittest.TestCase):
 def test_atomic_profile_edit_and_stale_guard(self):
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/'config.yaml';path.write_text('model:\n  default: configured-model\n  provider: fixture\nprivateSetting: keep\n')
   p=types.SimpleNamespace(home=root,model='configured-model',description='Role',display_name='Agent')
   before=apply(p,{'action':'read'})['profile']
   after=apply(p,{'action':'update','baseVersion':before['version'],'changes':{'name':'Changed','role':'New role'}})['profile']
   self.assertEqual(after['name'],'Changed');self.assertEqual(after['role'],'New role')
   self.assertIn('privateSetting: keep',path.read_text());saved=path.read_bytes()
   with self.assertRaises(ValueError):apply(p,{'action':'update','baseVersion':before['version'],'changes':{'name':'Stale'}})
   with self.assertRaises(ValueError):apply(p,{'action':'update','baseVersion':after['version'],'changes':{'name':'Invalid','modelPrimary':'not-configured'}})
   self.assertEqual(saved,path.read_bytes())
   self.assertEqual(apply(p,{'action':'read'})['profile']['version'],after['version'])
if __name__=='__main__':unittest.main()
