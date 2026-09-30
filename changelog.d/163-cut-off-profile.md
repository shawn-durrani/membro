- A profile the model didn't finish is no longer saved (#163). The
  profile writer thinks before it writes, and on 2 of 20 benchmark builds
  its thinking used up the 8,000-token cap, so the profile stopped
  mid-sentence and lost its newest sections. Each build call now gets
  16,000 tokens, or four per word of the budget if that's more, and the
  writer checks how every reply ended. A draft cut short keeps the
  profile you had, and a cut-short expand or squeeze pass keeps the
  draft it started from. The service log says why, with no profile text
  in it, and a rebuild started from the admin page reports the reason.
