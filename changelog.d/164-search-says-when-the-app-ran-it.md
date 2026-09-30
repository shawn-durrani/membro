- A history search can now say it was run by the chat app on your
  behalf, the way an automatic recall already does. `POST /search`
  takes an optional `origin` field, and `auto` marks such a search in
  the access log. The live view on the Mathematics page shows it as the
  app preparing context, so it no longer reads as a model choosing to
  dig into your past chats.
