// ---------------------------------------------------------------------------
//  layout — the arithmetic behind the banner and the CPU columns
//
//  Split out of main.cpp so tests/layout_test.cpp can exercise the code the
//  firmware actually runs. A test holding its own copy of a function is a test
//  that agrees with itself while the panel drifts away from both.
//
//  Both are templated on the two things that cannot follow them off the device:
//  the string type, and the function that measures text in the font currently
//  loaded. The firmware passes Arduino's String and TFT_eSPI's textWidth; the
//  test passes std::string and its own port of textWidth, driven by the metrics
//  in the real .vlw files. Templates rather than function pointers so the
//  firmware pays nothing for the seam.
// ---------------------------------------------------------------------------

#pragma once

#include <stdint.h>
#include <string.h>

// Greedy word wrap into at most maxLines lines of w pixels. Returns the lines
// written, and sets `cut` when the text did not all fit — whatever was left
// over is marked on the last line, so a message loses its tail visibly rather
// than spilling off the panel unannounced.
//
// Measured a word at a time into a char buffer. It used to ask the width of
// text.substring(pos, i + 1) for every i, which is a heap allocation and a
// fresh measurement of the whole prefix per character: quadratic, and a few
// dozen Strings churned per banner draw on a panel that redraws every second.
// A word boundary is the only place a greedy wrap can break anyway, so the
// character loop was measuring positions it could never use.
//
// The widths cannot simply be summed per character to avoid this. TFT_eSPI
// corrects the left bearing of the first glyph and ends on the last glyph's ink
// extent rather than its advance, so textWidth("ab") is not textWidth("a") plus
// textWidth("b") — the string has to be measured as a string.
//
// Runs of spaces collapse to one. Nothing notices: mac_stats_server already
// flattens whitespace before storing a message, and a wrapped line has no use
// for the difference.
template <typename Str, typename Measure>
int wrapText(const Str &text, int16_t w, int maxLines, Str *out,
             Measure measure, bool *cut = nullptr) {
  const int   n = (int)text.length();
  const char *s = text.c_str();

  char line[160];   // one rendered line; anything longer simply wraps again
  int  used = 0;
  int  pos  = 0;    // first character not yet placed

  while (pos < n && used < maxLines) {
    while (pos < n && s[pos] == ' ') pos++;
    if (pos >= n) break;

    int len   = 0;     // bytes committed to `line`
    int next  = pos;   // where the following line starts
    int probe = pos;

    while (probe < n) {
      int wordEnd = probe;
      while (wordEnd < n && s[wordEnd] != ' ') wordEnd++;

      const int sep = len ? 1 : 0;
      const int add = wordEnd - probe;
      if (len + sep + add + 1 > (int)sizeof(line)) break;

      if (sep) line[len] = ' ';
      memcpy(line + len + sep, s + probe, (size_t)add);
      line[len + sep + add] = '\0';

      if (measure(line) > w) {
        line[len] = '\0';   // that word overflowed: the line stands as it was
        break;
      }

      len += sep + add;
      next = wordEnd;
      while (next < n && s[next] == ' ') next++;
      probe = next;
    }

    // A single word wider than the line has no break point to find, so it gives
    // up characters until it fits. Bounded by the width of a line and reached
    // only by text with no spaces in it, which is why it can afford to measure
    // per character where the loop above cannot.
    if (len == 0) {
      int wordEnd = pos;
      while (wordEnd < n && s[wordEnd] != ' ') wordEnd++;
      while (len < wordEnd - pos && len + 1 < (int)sizeof(line)) {
        line[len]     = s[pos + len];
        line[len + 1] = '\0';
        if (measure(line) > w) { line[len] = '\0'; break; }
        len++;
      }
      if (len == 0) {   // not even one character fits; take it anyway
        line[0] = s[pos];
        line[1] = '\0';
        len = 1;
      }
      next = pos + len;
    }

    out[used++] = line;
    pos = next;
    while (pos < n && s[pos] == ' ') pos++;
  }

  if (cut) *cut = pos < n;

  if (pos < n && used > 0) {
    // Mark the loss on the last line, in the buffer for the same reason.
    Str &last = out[used - 1];
    int len = (int)last.length();
    if (len > (int)sizeof(line) - 3) len = (int)sizeof(line) - 3;
    memcpy(line, last.c_str(), (size_t)len);
    for (;;) {
      line[len]     = '.';
      line[len + 1] = '.';
      line[len + 2] = '\0';
      if (len <= 1 || measure(line) <= w) break;
      len--;
    }
    last = line;
  }
  return used;
}

// Column width and gap for the n core bars along the foot of the Mac page.
//
// This was a flat 22px, chosen for the eleven cores of the Mac in front of it.
// Anything wider ran off the panel and the draw loop cut the overflow off, so a
// sixteen-core machine drew twelve bars and silently dropped four. Gaps give
// way before bars do: past a dozen cores the separations would cost more of the
// panel than the things being separated.
struct CpuBars { int16_t bw, gap; };

static inline CpuBars cpuBarLayout(int n, int16_t width, int16_t margin,
                                   int16_t split) {
  if (n < 1) n = 1;
  CpuBars b;
  b.gap = n <= 12 ? 4 : n <= 20 ? 3 : 2;
  b.bw  = (int16_t)((width - 2 * margin - split - (n - 1) * b.gap) / n);
  if (b.bw > 22) b.bw = 22;   // a four-core machine should not get slabs
  if (b.bw < 2)  b.bw = 2;
  return b;
}
