"""Adapters that talk to an external model service.

Deliberately a subpackage. ``src/rag/*.py`` -- retrieval, context assembly,
citation validation, refusal and persistence -- must stay free of any network
client, and ``tests/test_rag.py`` asserts exactly that over ``src/rag/*.py``.
Only modules in *this* directory may import a vendor SDK, so the boundary is
visible in the file layout rather than resting on review discipline.
"""
