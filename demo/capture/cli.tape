# VHS scene: the same ranking in the terminal (insert for b4-pairings or the README loop).
# Media script, verified by viewing its frames; there is no unit test for it.
#
# Do not run this by hand: demo/capture/cli.sh builds the corpus, runs VHS with an
# empty environment and extracts the PNG sequence. The tape writes cli.mp4 into the
# work directory; VHS (v0.12) emits no PNG frames of its own and encodes 25fps
# whatever `Framerate` says, so cli.sh turns the MP4 into numbered PNGs and the
# composition re-times them to 60fps.
Output cli.mp4

Require python

Set Shell "bash"
Set Width 1920
Set Height 1080
Set Padding 72
# IBM Plex Mono (the brand mono) ships as woff2 only; install it system-wide and swap it in for the final take.
Set FontFamily "Liberation Mono"
Set FontSize 22
Set LineHeight 1.35
Set TypingSpeed 45ms
Set Framerate 60
Set CursorBlink false
# paper theme: tokens --canvas, --ink and the evidence colors from ui/tokens.css
Set Theme { "name": "orchestral-paper", "background": "#F7F8F8", "foreground": "#121417", "cursor": "#121417", "selectionBackground": "#E2E5E8", "black": "#121417", "brightBlack": "#69717A", "red": "#B04A00", "green": "#00775A", "yellow": "#8A5F00", "blue": "#005A8F", "magenta": "#4A5159", "cyan": "#005A8F", "white": "#F7F8F8", "brightWhite": "#FFFFFF", "brightRed": "#D55E00", "brightGreen": "#009E73", "brightYellow": "#E69F00", "brightBlue": "#0072B2", "brightMagenta": "#4A5159", "brightCyan": "#0072B2" }

Hide
Type "export PS1='$ ' NO_COLOR=" Enter
Type "cd $ORCH_REPO" Enter
Type "alias orch='python harness.py --runs-dir $ORCH_DEMO_RUNS --tasks-dir tests/fixtures/observatory/tasks --models-dir tests/fixtures/observatory/models'" Enter
Type "clear" Enter
Show

Sleep 600ms
Type "orch report --leaderboard"
Sleep 400ms
Enter
Sleep 4500ms
