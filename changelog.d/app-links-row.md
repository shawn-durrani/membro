- Your other apps are one tap away (workbench#100). A row at the top of
  the admin page links Crossband, Spendglass and Threadfold. Membro asks
  each one on this machine where a browser can open it, keeps the
  answers for a minute, and leaves out any app that doesn't answer. On
  the Mac every running app shows. From your phone only the apps served
  on your tailnet show, so the row never offers a link that won't open.
  The owner-only `GET /app-links` builds the row, and a new
  `sibling_apps` setting names the apps. The wire contract is unchanged,
  so `contract_version` stays where it is.
