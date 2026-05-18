#  Shared pytest fixtures (extend as needed)

import pytest

@pytest.fixture
def sample_xml():
    return """<?xml version="1.0"?>
<rpc-reply>
  <data><configuration><security>
    <address-book>
      <name>MORPHEUS_MANAGED</name>
      <address><name>10.0.0.1</name><ip-prefix>10.0.0.1/32</ip-prefix></address>
      <address><name>10.0.0.2</name><ip-prefix>10.0.0.2/32</ip-prefix></address>
      <address-set>
        <name>SET_WEB</name>
        <address><name>10.0.0.1</name></address>
        <address><name>10.0.0.2</name></address>
      </address-set>
    </address-book>
  </security></configuration></data>
</rpc-reply>"""