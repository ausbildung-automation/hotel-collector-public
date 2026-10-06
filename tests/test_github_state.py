import unittest
from github_state import Conflict, GitHubState, Transient


class Fake(GitHubState):
    def __init__(self, actions):
        super().__init__('owner/repo', 'main', 'token', sleeper=lambda _: None)
        self.actions = list(actions)

    def request(self, method, route, payload=None):
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class StateTests(unittest.TestCase):
    def test_read_retries_transient(self):
        import base64, json
        payload = base64.b64encode(json.dumps({'version': 1}).encode()).decode()
        f = Fake([Transient('x'), {'encoding': 'base64', 'content': payload, 'sha': 'a'}])
        data, sha = f.read('state.json')
        self.assertEqual(data['version'], 1)
        self.assertEqual(sha, 'a')

    def test_ambiguous_put_verified_by_readback(self):
        import base64, json
        data = {'version': 1, 'x': 2}
        payload = base64.b64encode(json.dumps(data).encode()).decode()
        f = Fake([Transient('maybe'), {'encoding': 'base64', 'content': payload, 'sha': 'new'}])
        result = f.write('state.json', data, 'old')
        self.assertTrue(result['verified_after_transient'])

    def test_ambiguous_put_never_overwrites_changed_state(self):
        import base64, json
        other = base64.b64encode(json.dumps({'version': 1, 'x': 9}).encode()).decode()
        f = Fake([Transient('maybe'), {'encoding': 'base64', 'content': other, 'sha': 'different'}])
        with self.assertRaises(Conflict):
            f.write('state.json', {'version': 1, 'x': 2}, 'old')


if __name__ == '__main__':
    unittest.main()
