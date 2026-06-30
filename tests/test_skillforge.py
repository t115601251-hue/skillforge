"""skillforge 测试套件。零外部依赖,只用标准库 unittest + unittest.mock。

跑法:
  cd skillforge dir
  python -m unittest discover tests -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import unittest
import urllib.error
from unittest import mock

import skillforge


def _mock_response(payload, headers=None):
    """共享工具:伪造一个 urllib urlopen 的 context-manager response。"""
    m = mock.MagicMock()
    m.read.return_value = json.dumps(payload).encode()
    m.status = 200
    m.headers = headers or {}
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def _raw_bytes(text):
    """共享工具:伪造一个返回纯文本的 urlopen response(模拟 raw.githubusercontent.com)。"""
    m = mock.MagicMock()
    m.read.return_value = text.encode()
    m.status = 200
    m.headers = {}
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class TestScaffolding(unittest.TestCase):
    def test_can_import(self):
        self.assertTrue(hasattr(skillforge, "scan_local"))


if __name__ == "__main__":
    unittest.main()
