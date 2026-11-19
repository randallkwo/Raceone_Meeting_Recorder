#!/usr/bin/env python3
"""RaceOne Meeting Recorder — 2026-10-02 PWA 修復影片 (90s, light bg)."""
from manim import *

class MeetingRecorderFix(Scene):
    def construct(self):
        # Brand colors from CIS v3.0
        bg = "#F9F9F6"
        primary = "#0066FF"
        accent = "#84CC16"
        text_color = "#1F2937"
        muted = "#6B7280"
        
        config.background_color = bg
        
        # ── Scene 1: title (3s) ──
        title = Text("RaceOne Meeting Recorder", font_size=44, color=text_color, weight=BOLD)
        sub = Text("PWA 修復紀錄 · 2026-10-02", font_size=22, color=muted)
        self.play(Write(title), run_time=1.2)
        self.play(Write(sub), run_time=0.8)
        self.wait(1)
        self.play(FadeOut(title), FadeOut(sub))
        
        # ── Scene 2: Problem statement (8s) ──
        prob = Text("問題", font_size=36, color=primary, weight=BOLD)
        self.play(Write(prob), run_time=0.6)
        
        items = [
            "PWA 內 target=\"_blank\" → 開新 webview，沒有上一頁",
            "使用者按完下載，頁面導航到新分頁 → 回不去",
            "手機長字串（URL/JSON）不斷行 → 橫向溢出",
        ]
        for i, txt in enumerate(items):
            bullet = Text(f"• {txt}", font_size=18, color=text_color)
            bullet.to_edge(LEFT, buff=0.6)
            self.play(Write(bullet), run_time=0.5)
            self.wait(0.3)
        self.wait(0.5)
        self.play(FadeOut(prob), *[FadeOut(bullet) for bullet in [bullet]])
        
        # ── Scene 3: Fix 1 - PWA nav (10s) ──
        fix1 = Text("修復 1：導航", font_size=32, color=primary, weight=BOLD)
        self.play(Write(fix1), run_time=0.5)
        
        box1 = RoundedRectangle(width=10, height=3, fill_color=bg, stroke_width=2, stroke_color=primary)
        txt1 = Text("target=\"_blank\" 全部改為 App 內按鈕", font_size=20, color=text_color)
        box1.move_to(ORIGIN)
        txt1.move_to(ORIGIN)
        self.play(Create(box1), Write(txt1), run_time=1)
        self.wait(0.5)
        self.play(FadeOut(fix1), FadeOut(box1), FadeOut(txt1))
        
        # ── Scene 4: Fix 2 - word break (10s) ──
        fix2 = Text("修復 2：文字斷行", font_size=32, color=primary, weight=BOLD)
        self.play(Write(fix2), run_time=0.5)
        
        # Show before/after comparison
        before_box = RoundedRectangle(width=10, height=2, fill_color="#FEE2E2", stroke_width=1, stroke_color="#C62828")
        before_text = Text("Before: 1200px 溢出 3× ❌", font_size=20, color="#C62828")
        before_box.move_to(LEFT * 0.5)
        before_text.move_to(LEFT * 0.5)
        
        after_box = RoundedRectangle(width=10, height=2, fill_color="#BBF7D0", stroke_width=1, stroke_color="#15803D")
        after_text = Text("After: 390px 不溢出 ✅", font_size=20, color="#15803D")
        after_box.move_to(RIGHT * 0.5)
        after_text.move_to(RIGHT * 0.5)
        
        self.play(Create(before_box), Write(before_text), run_time=0.8)
        self.wait(0.3)
        self.play(Create(after_box), Write(after_text), run_time=0.8)
        self.wait(1)
        self.play(FadeOut(fix2), FadeOut(before_box), FadeOut(before_text), FadeOut(after_box), FadeOut(after_text))
        
        # ── Scene 5: Fix 3 - safe area (8s) ──
        fix3 = Text("修復 3：iPhone Home Indicator", font_size=30, color=primary, weight=BOLD)
        self.play(Write(fix3), run_time=0.5)
        txt3 = Text("底部工具列加 safe-area-inset-bottom", font_size=18, color=text_color)
        self.play(Write(txt3), run_time=0.5)
        self.wait(0.5)
        self.play(FadeOut(fix3), FadeOut(txt3))
        
        # ── Scene 6: Deploy (6s) ──
        fix4 = Text("已部署", font_size=32, color=accent, weight=BOLD)
        self.play(Write(fix4), run_time=0.5)
        status = Text("your-domain.com → HTTP 200 / 0.13s", font_size=18, color=text_color)
        self.play(Write(status), run_time=0.5)
        self.wait(1)
        
        # ── Scene 7: GitHub repos (8s) ──
        gh = Text("GitHub 已同步", font_size=30, color=primary, weight=BOLD)
        self.play(Write(gh), run_time=0.5)
        
        repo1 = Text("RaceoneMeetingAPP (私有) → ce7a5d2", font_size=16, color=muted)
        repo2 = Text("Raceone_Meeting_Recorder (公開) → 255d01a", font_size=16, color=muted)
        repo1.to_edge(LEFT, buff=0.5)
        repo2.to_edge(RIGHT, buff=0.5)
        self.play(Write(repo1), run_time=0.5)
        self.play(Write(repo2), run_time=0.5)
        self.wait(1)
        self.play(FadeOut(gh), FadeOut(repo1), FadeOut(repo2))
        
        # ── Scene 8: CTA (5s) ──
        cta = Text("重新開啟 App 即見新版", font_size=28, color=text_color, weight=BOLD)
        note = Text("Cache-Control: no-store + Service Worker 不快取 /", font_size=16, color=muted)
        self.play(Write(cta), run_time=0.8)
        self.play(Write(note), run_time=0.5)
        self.wait(2)
        
        # ── End ──
        self.play(FadeOut(cta), FadeOut(note))
        end = Text("RaceOne — 守護每一位跑者的旅程", font_size=24, color=primary, weight=BOLD)
        self.play(Write(end), run_time=0.8)
        self.wait(2)
