-- Three talks with many tasks got a cut-off reply on the first short-names run (reply limit too small, fixed): try again.
UPDATE conversation SET short_title = NULL WHERE short_title = '';
