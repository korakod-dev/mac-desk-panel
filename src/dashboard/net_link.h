// ---------------------------------------------------------------------------
//  net_link — two ways onto the network, picked automatically
//
//    USB   tools/usb_net_bridge.py, running on the machine at the other end of
//          the USB-C cable, answers requests framed over the same CDC link that
//          already carries the console. The host does the DNS, the TLS and the
//          fetch; the panel only sees the response body. No radio involved.
//
//    WiFi  the panel's own station interface, used whenever no bridge answers.
//
//  The USB link shares Serial with the log output and the framebuffer dump, so
//  this module owns everything arriving on Serial. Single-byte UI commands that
//  are not part of a response come back out through readCommand().
// ---------------------------------------------------------------------------

#pragma once

#include <Arduino.h>

namespace net {

enum class Via : uint8_t { None, Usb, Wifi };

// Brings up WiFi if secrets.h holds real credentials. The USB bridge needs no
// setup — it is detected by being answered.
void begin();

// Pumps the serial reader and pings the bridge. Cheap; call it every loop.
void loop();

Via         via();        // link that will be used for the next request
const char *viaName();    // "USB" / "WiFi" / "OFFLINE"
bool        online();

// Blocking, with the same shape as the HTTPClient calls this replaces: true and
// the body in `out`, or false and a short reason in `err`.
bool get(const String &url, String &out, String &err);

// Wall clock from the bridge, for when there is no WiFi to reach NTP over.
bool syncTimeFromHost();

// Console bytes the host typed, one at a time. -1 when the queue is empty.
int readCommand();

}  // namespace net
