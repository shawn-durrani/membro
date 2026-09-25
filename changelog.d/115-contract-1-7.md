- Memory contract 1.7 (#115): a fact a model saves while a guest is in
  the room now stays in that chat once you approve it. It was held for
  review, but after approval it was recalled in every chat, because the
  save never said which chat it came from. `POST /facts` now takes the
  same `source_app` and `conversation_id` pair as `/ingest` and
  `/recall`, and a save carrying `guest_speakers` binds to that chat,
  like a fact mined from a guest's own words. A save made before the
  chat's first handoff creates the chat's record, so it binds from the
  start. Your own saves stay global. A 1.6 client keeps working
  unchanged, and its guest-present saves stay global as before.
