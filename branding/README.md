# branding/

Logos and icons of an instance. `BRANDING_DIR` in `.env` names the folder to use; `./homelab configure`
copies its files to where they are used (all gitignored there).

- `default/`: a neutral set (a house on teal) for a new instance. For your own: copy it to
  `branding/<your slug>/`, replace the images (same names and sizes), set `BRANDING_DIR=branding/<your slug>`,
  then `./homelab configure`.
- Your own set can also live in your instance's repository (`BRANDING_DIR=instance/branding/<slug>`).

| File | Size | Copied to | Used for |
|---|---|---|---|
| `mail-logo.png` | about 600×689, transparent | `authentik/mail-logo.png` | inline logo in Authentik's mails (`cid:logo`) |
| `site/logo.png`, `site/logo-light.png` | 600×689 | `site/` | dark artwork for light backgrounds (Authentik's brand logo uses `logo-%(theme)s.png`) |
| `site/logo-dark.png`, `site/logo-white.png` | 600×689 | `site/` | white artwork for dark backgrounds |
| `site/mark.png` | 512×512 | `site/` | the home page's brand mark; the family app's sidebar |
| `site/favicon.png` | 64×64 | `site/` | browser tab (home page and Authentik) |
| `site/apple-touch-icon.png` | 180×180, no transparency | `site/` | iPhone home screen |
| `site/icon-192.png`, `site/icon-512.png` | no transparency, full bleed | `site/` | web app manifest (Android, "maskable") |
| `app/32x32.png` … `app/128x128@2x.png`, `app/icon.png` | as named, `icon.png` 512 | `family-app/src-tauri/icons/` | family app window and bundle |
| `app/icon.icns`, `app/icon.ico` | multi-size | `family-app/src-tauri/icons/` | macOS / Windows bundle icon |
| `app/tray.png`, `app/tray-down.png` | 64×64 | `family-app/src-tauri/icons/` | tray icon: all fine / something isn't working |
