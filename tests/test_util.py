import unittest
from mailpile.util import b64w, b64c


class TestUtil(unittest.TestCase):

    def test_b64w(self):
        self.assertEquals("abc123456def", b64w("abc123456def"))
        self.assertEquals("a-b-c-123-", b64w("a+b+c+123+"))

    def test_b64c(self):
        self.assertEquals("abc123456def", b64c("abc123456def"))
        self.assertEquals("a_bc_", b64c("\na/=b=c/"))
        self.assertEquals("a+b+c+123+", b64c("a+b+c+123+"))
