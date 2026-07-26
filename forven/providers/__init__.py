"""Provider-facing subpackage.

ARCH-06: home for the LLM/data provider registry that used to live inline in
``forven.api_core``. Deliberately empty of imports — importing this package must
never pull in FastAPI, the DB, or httpx clients; consumers import the concrete
module (``forven.providers.discovery``) they need.
"""
