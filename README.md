# loiolink

Send video URLs and text to Kodi from any browser on your local network. Includes an API for integration with other addons. No app needed — just paste and send.

PLANNED FEATURES
-----------------

1. QR Code on screen
   Show a QR code on the Kodi screen so you don't have to type the IP.
   Just scan it with your phone camera and the web opens directly.

2. Custom themes
   Addons that use LoioLink can pass their own colors and logo.
   The web page shows their branding, not mine.

3. History
   Remember previously sent URLs and text.
   Quick access to resend something you already used.

4. Multi-language
   Detect Kodi's language and serve the web page in that language.
   Spanish, English, French, German, Portuguese, etc.

5. Queue / multiple sends
   Send several URLs without having to reopen the server each time.
   Basically a playlist builder from your phone.

6. Auto-updates
   If you copy the code, you don't get my fixes and improvements.
   If you use the addon as a dependency, it updates automatically.


API FOR DEVELOPERS
-------------------

I want other Kodi addon developers to be able to use LoioLink
in their own addons without having to rewrite the whole thing.

How it works:

1. Add this to your addon.xml:
   <import addon="plugin.program.loiolink"/>

2. In your Python code:
   from loiolink import remote
   text = remote.receive_text("Search in My Addon")


   <import addon="plugin.program.loiolink"/>
Llamadas:
receive_text()
 o 
start_remote()


That's it. Two lines. LoioLink handles the server, the web page,
the waiting, and gives you back the text. You don't need to deal
with HTTP servers, HTML, threading, or port management.

