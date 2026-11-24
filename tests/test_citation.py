import json,unittest,copy
from pathlib import Path
from citation_lineage import process
class T(unittest.TestCase):
 def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
 def test_lineage(self):
  r=process(self.d);self.assertEqual(r['roots'],['d3']);self.assertEqual(r['summary']['max_depth'],2)
 def test_cycle_detected(self):
  self.d['citations'].append({'from':'d1','to':'d3','page':'p1'});self.assertEqual(len(process(self.d)['cycles']),1)
 def test_deterministic(self):
  r=process(self.d);self.d['citations'].reverse();self.assertEqual(process(self.d),r)
 def test_bad_ref(self):
  self.d['citations'][0]['to']='x'
  with self.assertRaises(ValueError):process(self.d)
 def test_self_ref(self):
  self.d['citations'].append({'from':'d1','to':'d1','page':'x'})
  with self.assertRaises(ValueError):process(self.d)
 def test_duplicate_doc(self):
  self.d['documents'].append(self.d['documents'][0])
  with self.assertRaises(ValueError):process(self.d)
