// ---------------------------------------------------------------------------
//  Host-side tests for src/dashboard/layout.h
//
//  Run with tests/run.sh. Nothing here is mocked except the two things that
//  cannot leave the device: the string type and the width of a piece of text.
//  wrapText and cpuBarLayout are the firmware's own, included from the header
//  it compiles from.
//
//  The width function is not a stand-in either — it is TFT_eSPI's textWidth
//  transcribed from Extensions/Smooth_font.cpp and TFT_eSPI.cpp, reading the
//  metrics out of the real .vlw files this project generates. That matters more
//  than it sounds: the rule is not "sum of the character widths". The first
//  glyph's negative left bearing is added back and the last glyph contributes
//  its ink extent rather than its advance, so a wrap tested against a simpler
//  model would pass here and overflow on the panel.
// ---------------------------------------------------------------------------

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>
#include <random>

#include "../src/dashboard/layout.h"

// --- the font, loaded the way TFT_eSPI loads it ------------------------------

struct Font {
    std::vector<uint32_t> uni;
    std::vector<uint8_t>  w, adv;
    std::vector<int8_t>   dX;
    int ascent = 0, descent = 0, spaceWidth = 0;
    std::string name;
};

static uint32_t be32(const uint8_t *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static Font loadVlw(const char *path) {
    Font f;
    f.name = path;
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        fprintf(stderr,
                "\ncannot open %s\n\n"
                "The .vlw files are build byproducts and gitignored. Regenerate\n"
                "them (this also rewrites the identical .h) with:\n\n"
                "  .venv/bin/python tools/make_vlw.py \\\n"
                "      tools/fonts/IBMPlexSansThai-Regular.ttf \\\n"
                "      16 src/fonts/ui16.h UiFont16 --set ascii\n"
                "  ... and the same for ui24 (24px) and big64 (64px, --set numeric)\n\n",
                path);
        exit(2);
    }
    std::vector<uint8_t> b;
    uint8_t tmp[4096];
    size_t n;
    while ((n = fread(tmp, 1, sizeof(tmp), fp)) > 0) b.insert(b.end(), tmp, tmp + n);
    fclose(fp);

    int count = (int)be32(&b[0]);
    f.ascent  = (int)be32(&b[16]);
    f.descent = (int)be32(&b[20]);
    for (int i = 0; i < count; i++) {
        const uint8_t *m = &b[24 + i * 28];
        f.uni.push_back(be32(m));
        f.w  .push_back((uint8_t)be32(m + 8));
        f.adv.push_back((uint8_t)be32(m + 12));
        f.dX .push_back((int8_t)be32(m + 20));
    }
    f.spaceWidth = (f.ascent + f.descent) * 2 / 7;   // Smooth_font.cpp:247
    return f;
}

static const Font *g_font = nullptr;

// TFT_eSPI::textWidth, SMOOTH_FONT branch (TFT_eSPI.cpp:3079-3098). This and
// the spaceWidth line above are transcribed from TFT_eSPI, copyright (c) Bodmer
// (https://github.com/Bodmer), FreeBSD licence — which asks that the notice
// travel with source taken from it, so it travels here.
static int16_t measure(const char *s) {
    int32_t width = 0;
    while (*s) {
        uint32_t u = (uint8_t)*s++;
        if (u == 0x20) { width += g_font->spaceWidth; continue; }
        int gn = -1;
        for (size_t i = 0; i < g_font->uni.size(); i++)
            if (g_font->uni[i] == u) { gn = (int)i; break; }
        if (gn < 0) { width += g_font->spaceWidth + 1; continue; }
        if (width == 0 && g_font->dX[gn] < 0) width -= g_font->dX[gn];
        if (*s) width += g_font->adv[gn];
        else    width += g_font->dX[gn] + g_font->w[gn];
    }
    return (int16_t)width;
}

// --- checking ----------------------------------------------------------------

static int failures = 0, checks = 0;

static void fail(const char *what, const std::string &detail) {
    if (++failures <= 12) printf("  FAIL %-22s %s\n", what, detail.c_str());
}

// Spaces removed. Comparing on this rather than on the words is what lets the
// check cover both kinds of break: between words, where a space disappears, and
// inside one too long to fit, where it does not. Either way the characters that
// come out must be the characters that went in.
static std::string squash(const std::string &s) {
    std::string out;
    for (char c : s) if (c != ' ') out += c;
    return out;
}

// Every property a wrap has to hold, whatever the input.
static void checkWrap(const std::string &in, int16_t w, int maxLines) {
    std::string out[4];
    bool cut = false;
    int n = wrapText(in, w, maxLines, out, measure, &cut);
    checks++;

    char ctx[256];
    snprintf(ctx, sizeof(ctx), "w=%d lines=%d in=\"%.60s\"", w, maxLines, in.c_str());

    if (n < 0 || n > maxLines) { fail("line count", ctx); return; }

    std::string joined;
    for (int i = 0; i < n; i++) {
        const std::string &ln = out[i];
        if (measure(ln.c_str()) > w) {
            fail("line overflows", std::string(ctx) + " -> \"" + ln + "\" " +
                                   std::to_string(measure(ln.c_str())) + "px");
        }
        if (ln.empty())                              fail("empty line", ctx);
        else if (ln.front() == ' ' || ln.back() == ' ')
            fail("line has edge space", std::string(ctx) + " -> \"" + ln + "\"");
        if (ln.find("  ") != std::string::npos)
            fail("line has double space", std::string(ctx) + " -> \"" + ln + "\"");
        joined += ln;
    }

    // Nothing invented, nothing dropped, nothing reordered: entire when it
    // fitted, a prefix when it did not.
    const std::string want = squash(in);

    if (!cut) {
        if (squash(joined) != want)
            fail("content changed", std::string(ctx) + " -> \"" + joined + "\"");
    } else if (n > 0) {
        if (joined.size() < 2 || joined.compare(joined.size() - 2, 2, "..") != 0) {
            fail("cut unmarked", std::string(ctx) + " -> \"" + joined + "\"");
        } else {
            const std::string body = squash(joined.substr(0, joined.size() - 2));
            if (body.size() > want.size() ||
                want.compare(0, body.size(), body) != 0)
                fail("cut not a prefix", std::string(ctx) + " -> \"" + body + "\"");
        }
    }
}

// --- the cases ----------------------------------------------------------------

static const char *CORPUS[] = {
    "",
    " ",
    "   leading and trailing   ",
    "x",
    "build failed",
    "Claude needs your input",
    "T-Display-S3: done in 3m 12s",
    "Claude is waiting for you to approve a permission before it can finish",
    "one  two   three    four",
    "a b c d e f g h i j k l m n o p q r s t u v w x y z 0 1 2 3 4 5",
    "supercalifragilisticexpialidociousandthensomemoreletterswithnobreaks",
    "/Users/korakod/Library/Application-Support/mac-stats-server/longpath.py",
    "WWWWW MMMMM WWWWW MMMMM WWWWW MMMMM WWWWW MMMMM WWWWW MMMMM WWWWW MMMM",
    "31 degrees, feels like 38 - that gap is the whole point of the line",
    "error: cannot allocate the frame buffer, PSRAM exhausted at 0x3fc80000",
};

int main() {
    Font ui16 = loadVlw("src/fonts/ui16.vlw");
    Font ui24 = loadVlw("src/fonts/ui24.vlw");
    Font *fonts[] = {&ui16, &ui24};

    // The banner's real geometry: drawBanner uses w = SCR_W - 16 and
    // tw = w - 24, so 280px, at ui24 over two lines then ui16 over three.
    const int16_t BANNER_W = 280;

    printf("wrapText\n");

    for (Font *f : fonts) {
        g_font = f;
        for (const char *t : CORPUS) {
            checkWrap(t, BANNER_W, 2);
            checkWrap(t, BANNER_W, 3);
            // narrower boxes than the banner ever uses, to push the edges
            for (int16_t w = 60; w <= 300; w += 20) checkWrap(t, w, 1 + w % 3);
        }
        // every prefix of a long message, to sweep the boundary lengths
        std::string base =
            "Claude Code finished the turn you started twelve minutes ago now";
        for (size_t i = 1; i <= base.size(); i++)
            checkWrap(base.substr(0, i), BANNER_W, 3);
    }
    printf("  %d cases from the corpus\n", checks);

    // Random text, at the widths the banner actually uses and below.
    int before = checks;
    std::mt19937 rng(20260809);
    const char *alpha =
        " abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,:-/%  ";
    const size_t alphaLen = strlen(alpha);
    for (int i = 0; i < 200000; i++) {
        g_font = fonts[rng() % 2];
        std::string t;
        int len = (int)(rng() % 100);
        for (int j = 0; j < len; j++) t += alpha[rng() % alphaLen];
        checkWrap(t, (int16_t)(60 + rng() % 240), 1 + (int)(rng() % 3));
    }
    printf("  %d random cases\n", checks - before);

    // --- cpuBarLayout ---------------------------------------------------------
    //
    // The property that was broken: every core the host reports has to land on
    // the panel. pageMac draws bar i at x0 + i*(bw+gap), plus SPLIT once the
    // performance cluster starts, and skips any that would cross the margin.

    printf("cpuBarLayout\n");
    const int16_t SCR_W = 320, MARGIN = 4;
    int worst = 0;
    for (int split01 = 0; split01 <= 1; split01++) {
        for (int n = 1; n <= 32; n++) {
            const int16_t SPLIT = split01 ? 10 : 0;
            CpuBars b = cpuBarLayout(n, SCR_W, MARGIN, SPLIT);
            checks++;

            if (b.bw < 2 || b.gap < 1) {
                fail("degenerate bars", "n=" + std::to_string(n));
                continue;
            }

            int drawn = 0;
            int16_t span = (int16_t)(n * b.bw + (n - 1) * b.gap + SPLIT);
            int16_t x0 = (int16_t)((SCR_W - span) / 2);
            if (x0 < MARGIN) x0 = MARGIN;
            for (int i = 0; i < n; i++) {
                bool perf = split01 && i >= n / 2;
                int16_t x = (int16_t)(x0 + i * (b.bw + b.gap) + (perf ? SPLIT : 0));
                if (x + b.bw > SCR_W - MARGIN) break;
                drawn++;
            }
            if (drawn != n)
                fail("cores dropped", "n=" + std::to_string(n) + " split=" +
                                      std::to_string(split01) + " drew " +
                                      std::to_string(drawn));
            if (n == 11 && split01) worst = b.bw;
        }
    }
    printf("  1..32 cores, split and not\n");

    // The Mac this was built for. Its layout predates the fitting and should
    // survive it unchanged, or the panel in front of you moves for no reason.
    if (worst != 22) {
        fail("11-core layout moved", "bw=" + std::to_string(worst) + " want 22");
    } else {
        printf("  11 cores still 22px wide\n");
    }

    printf("\n%d checks, %d failures\n", checks, failures);
    if (failures > 12) printf("(%d more not shown)\n", failures - 12);
    return failures ? 1 : 0;
}
