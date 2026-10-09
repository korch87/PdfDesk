# PdfDesk

PDF viewer and editor under development for Windows and macOS.

## Intel macOS test installer

Run **Actions → Build macOS DMG (Intel) → Run workflow**.
After a successful run, download the `PdfDesk-macos-Intel-DMG` artifact,
extract its outer ZIP and open the DMG. Drag PdfDesk into Applications.

This is a test build. macOS compatibility and document workflows still need
real-device testing. The current build uses ad-hoc signing and is not notarized;
Gatekeeper may prevent launch when downloaded from the internet.
Mac OCR and Office conversion currently require separately installed engines.

No user PDFs, recovery data, local settings or Windows binaries are included
in this repository. No application license has been selected yet.
