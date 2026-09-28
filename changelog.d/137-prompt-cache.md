- Mining and the profile now reuse Anthropic's prompt cache when their
  calls come close together (#137). The miner's instructions and its list
  of your existing facts are laid out to stay the same from one call to
  the next, so in a burst (a backlog, an import) every call after the
  first reads them at a tenth of the price. The same facts, rules and
  answer format reach the model. A profile build's expansion pass reads
  the entries its draft just sent. Each model call also writes one line
  to `data/service.log` with its call site and token counts, never text,
  so you can see how much of the input came from the cache.
