.. _index:

``schwaby``: An Unofficial Charles Schwab API Client
====================================================

.. image:: _static/github-logo.png
   :width: 40
   :target: https://github.com/tom-hirt/schwaby

``schwaby`` is an unofficial Python client for the Charles Schwab trading API.
It covers every endpoint Schwab publishes, turns the streaming websocket into
something you can read, builds orders Schwab will accept, and takes the sharp
edges off the parts that matter when the account is funded.

.. code-block:: shell

  pip install schwaby

The distribution and the package are both ``schwaby``, so ``pip install
schwaby`` then ``import schwaby``.

Once you have a token, the shape of it is this:

.. code-block:: python

  from schwaby.auth import client_from_token_file

  c = client_from_token_file(
          token_path='/path/to/token.json',
          api_key='APIKEY',
          app_secret='APP_SECRET')

  r = c.get_quote('AAPL')
  r.raise_for_status()
  print(r.json())

New here? Start with :ref:`getting_started`, then :ref:`auth`.

.. danger::

   **This places real orders with real money, and it has bugs.**

   Not "may have". Every defect ever found in this library was in code with
   100% test coverage, and the ones that mattered were *silent* --- a limit
   price a cent low, an option symbol naming a different contract, an order
   routed to a venue nobody asked for. None raised. None failed a test. The
   `changelog
   <https://github.com/tom-hirt/schwaby/blob/main/CHANGELOG.md>`__ lists them,
   and the next one is in there somewhere too, unfound.

   **You are responsible for every order your code places.** Not the author,
   not the maintainer, not anyone who has ever contributed. If this library
   loses you money, the loss is yours. Nothing here is financial advice. There
   is no undo; a wrong order is filled before you know it was wrong.

   Start with size you can afford to lose entirely and stay there longer than
   feels necessary. Reconcile against the broker rather than trusting what this
   library tells you happened. Read the code on every path that places,
   replaces or cancels an order. And assume the bug you have not found is on
   the path you did not read.

.. toctree::
  :maxdepth: 2
  :caption: Contents:

  getting-started
  auth
  client
  streaming
  order-templates
  order-builder
  util
  api
  help
  contributing


--------------------
Where this came from
--------------------

``schwaby`` began from `alexgolec/schwab-py
<https://github.com/alexgolec/schwab-py>`__, an MIT-licensed library by Alex
Golec which gave this project its shape and most of its code. It became a
separate project to prioritise what matters when real orders depend on it ---
prices that are never silently altered, a token file that survives a crash, a
stream that reports what it absorbs. Alex Golec's copyright
notice is retained unchanged and the licence is the same MIT one he chose.

See the `changelog
<https://github.com/tom-hirt/schwaby/blob/main/CHANGELOG.md>`__ for what
changed, and the `README <https://github.com/tom-hirt/schwaby>`__ for the
longer version.


-----------------------
Disclaimer and licence
-----------------------

``schwaby`` is an unofficial API wrapper, in no way endorsed by or affiliated
with Charles Schwab or any associated organization. Read and understand the
terms of service of the underlying API before using it.

The software is provided **as is, without warranty of any kind**. The author,
the maintainer and every contributor accept no responsibility or liability for
any loss, damage, missed trade, unintended order, or any other consequence
whatsoever arising from its use --- financial or otherwise, foreseeable or not.
Using it against a funded account is entirely at your own risk.

The ``LICENSE`` file carries the binding text; where this page and that file
differ, that file governs.
