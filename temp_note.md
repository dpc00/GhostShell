  Not applicable as-is: GPU/Metal/OpenGL rendering, custom shaders, native macOS/GTK shell, quick-terminal dropdown, terminal
  inspector GUI, sixel/Kitty graphics protocol, tmux integration, split-pane windowing, crash telemetry — these all rely on
  Ghostty's standalone GPU-surface app model, so none of that code carries over to rendering inside a Sublime Text buffer.
  Equivalent features (e.g. split panes, session persistence) aren't ruled out — they'd just have to be built new against
  Sublime's own APIs rather than reused from Ghostty.
