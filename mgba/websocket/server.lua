-- Derivato da lua-websockets (https://github.com/lipp/lua-websockets)
-- Copyright (c) 2012 by Gerhard Lipp <gelipp@gmail.com> - licenza MIT (testo completo nel
-- file COPYRIGHT di lua-websockets). Preso via Celio-mGBA-Link (GPL-3.0). Vedi THIRD_PARTY_NOTICES.md.

local insert = table.insert
local remove = table.remove

local clientFactory = require'websocket.server_client'

local listener = {}
local clients = {}

--////////////////////////////////////////////////////////////////////////////////////////////////////////--

local listen = function(opts)
  local self = {}

  assert(opts and (opts.protocols or opts.default))

  console:log("WS: Start listening on port " .. opts.port)
  listener = socket.bind(nil , opts.port)

  if not listener then
    console:error("WS: Listening filed with error. Port already in use?")
    return
  else
    console:log("WS: Listening...")
  end

  listener:add("received", function() self.socket_accept() end)
  listener:listen()

  self.sock = function()
    return listener
  end

  self.close = function(keep_clients)
    listener:close()
    listener = nil
    if not keep_clients then
      for client in clients do
        client:close()
      end
    end
  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  self.socket_accept = function()
    local client_sock = listener:accept()

    if not client_sock then
      console:error("WS: Error when accepting connection")
      return
    end

    local client = clientFactory.create_client(client_sock, opts)

    client:set_on_close(
      function(handler, was_clean, code, reason)
        for index, tempClient in ipairs(clients) do
          if (tempClient == handler) then
            remove(clients, index)
          end
        end
      end
    )

    insert(clients, client)

  end

  --////////////////////////////////////////////////////////////////////////////////////////////////////////--

  return self
end

--////////////////////////////////////////////////////////////////////////////////////////////////////////--

return {
  listen = listen
}

--////////////////////////////////////////////////////////////////////////////////////////////////////////--
