- The miner no longer sees other chats' guest facts (#139). When it mines
  a chat, it's shown your existing facts so it doesn't save one twice, and
  that list held every chat's facts, guests' facts included. It now holds
  what a recall from that chat would find: the facts every chat can see,
  and the ones bound to this chat. So a guest's fact from one chat can't
  pass for a repeat, or be marked replaced, by something said in another.
  The list keeps its cache-friendly layout, so a burst of mining calls
  still shares the prompt cache.
