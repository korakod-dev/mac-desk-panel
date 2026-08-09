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
//
// `timeoutMs` is how long to wait before giving up, 0 for the default. It is
// worth passing: the default is sized for a weather fetch the host has to do
// DNS and TLS for, and spending that on a service running on the host itself is
// how a dead bridge turns into eight seconds of frozen panel.
//
// `token`, when given, is sent as X-Panel-Token — and only over WiFi, which is
// the only path that needs it. A request over the bridge is made by the bridge
// itself and arrives at the servers from loopback, which they trust; a request
// over the radio arrives from the LAN, which they no longer do. Pass it only
// for the host's own services: sending it to Open-Meteo would hand a third
// party a secret for nothing.
bool get(const String &url, String &out, String &err, uint32_t timeoutMs = 0,
         const char *token = nullptr);

// Wall clock from the bridge, for when there is no WiFi to reach NTP over.
bool syncTimeFromHost();

// Console bytes the host typed, one at a time. -1 when the queue is empty.
int readCommand();

// Called repeatedly while a request is blocked waiting on the USB link, so the
// panel keeps its clock and its buttons through a fetch instead of freezing for
// the duration. It must not fetch anything itself — a nested request would take
// the reader out from under the one already waiting — and requests made from
// inside it fail immediately rather than being allowed to try.
using IdleHook = void (*)();
void setIdleHook(IdleHook fn);

}  // namespace net
