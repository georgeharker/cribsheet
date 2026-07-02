-- Dump resolved LSP server specs from the user's nvim config as JSON.
-- Run: nvim --headless -u <their init> -l dump_lsp.lua basedpyright rust_analyzer …
-- vim.lsp.config[name] is the merged (lspconfig defaults + user overrides + '*') spec.
local out = {}
for _, name in ipairs(_G.arg) do
  local ok, c = pcall(function() return vim.lsp.config[name] end)
  if ok and c then
    out[name] = {
      cmd          = type(c.cmd) == "table" and c.cmd or nil,
      cmd_env      = c.cmd_env,
      filetypes    = c.filetypes,
      root_markers = c.root_markers,
      settings     = c.settings,
      init_options = c.init_options,
    }
  end
end
io.write(vim.json.encode(out))
