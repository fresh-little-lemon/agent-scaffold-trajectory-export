U=http://127.0.0.1:18005/v1/messages; H='-H content-type:application/json -H anthropic-version:2023-06-01'
req() { echo "== $1"; curl -s -N --noproxy '*' -o /tmp/proxyexp_gsy/r.out -w "HTTP %{http_code} ctype=%{content_type}\n" $U $H -d "$2"; head -c 700 /tmp/proxyexp_gsy/r.out | tr '\n' ' ' ; echo; echo "... tail:"; tail -c 400 /tmp/proxyexp_gsy/r.out | tr '\n' ' '; echo; }
req "non-stream: bad param (max_tokens=-5)" '{"model":"deepseek-v4.1-flash","max_tokens":-5,"messages":[{"role":"user","content":"hi"}]}'
req "stream: bad param (max_tokens=-5)" '{"model":"deepseek-v4.1-flash","max_tokens":-5,"stream":true,"messages":[{"role":"user","content":"hi"}]}'
req "non-stream: prompt too long" "{\"model\":\"deepseek-v4.1-flash\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"$(python3 -c 'print("word "*300000)')\"}]}"
req "stream: prompt too long" "{\"model\":\"deepseek-v4.1-flash\",\"max_tokens\":10,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"$(python3 -c 'print("word "*300000)')\"}]}"
req "non-stream: max_tokens hit" '{"model":"deepseek-v4.1-flash","max_tokens":8,"messages":[{"role":"user","content":"Write a long poem."}]}'
req "stream: max_tokens hit" '{"model":"deepseek-v4.1-flash","max_tokens":8,"stream":true,"messages":[{"role":"user","content":"Write a long poem."}]}'
