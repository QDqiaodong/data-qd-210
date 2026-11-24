import unittest,json
from pathlib import Path
from citation_lineage import process
class Regression(unittest.TestCase):
 def test_cycle_depth_and_long_chain(self):
  d=json.loads(Path('fixtures/input.json').read_text());d['citations'].append({'from':'d1','to':'d3','page':'x'});r=process(d)
  self.assertIsNone(r['summary']['max_depth']);self.assertEqual(r['cycles'],[['d1','d2','d3']]);self.assertEqual(r['cited_ancestors']['d1'],['d2','d3'])
  d={'documents':[{'id':'d'+str(i),'title':'x','date':'x'} for i in range(1000)],'citations':[{'from':'d'+str(i),'to':'d'+str(i+1),'page':'p'} for i in range(999)]}
  self.assertEqual(process(d)['summary']['max_depth'],999)
