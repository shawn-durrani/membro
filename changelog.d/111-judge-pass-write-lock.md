- The judge pass no longer locks other writes out while it waits on the
  model. It used to hold the database's write lock for the whole pass,
  so a slow network, or the computer sleeping mid-pass, made incoming
  chats fail with "database is locked" and history searches hang. Each
  fact's verdict now saves straight after its own model call. A search
  whose access-log write meets a busy lock also stops waiting twice
  before it answers (#111).
