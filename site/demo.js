window.FDEMO = {
 "start": {
  "nodes": {},
  "stamps": {},
  "verdict": [
   "fetch holds its hosts in the binary. Nothing here is a flag.",
   ""
  ]
 },
 "items": [
  {
   "l": [
    "d",
    "$ fetch introspect | jq -c '.authority.labels[] | select(.name == \"net_out\")'"
   ]
  },
  {
   "l": [
    "a",
    "{\"name\":\"net_out\",\"argument\":\"127.0.0.1:18090\",\"bounded\":true}"
   ],
   "s": {
    "nodes": {},
    "stamps": {},
    "verdict": [
     "One host in the row: 127.0.0.1:18090. That is all it can dial.",
     ""
    ]
   }
  },
  {
   "l": [
    "d",
    "$ fetch get /v1/items --host 127.0.0.1:18090"
   ]
  },
  {
   "l": [
    "g",
    "{\"method\":\"get\",\"scheme\":\"http\",\"host\":\"127.0.0.1:18090\",\"path\":\"/v1/items\",\"status\":200,\"body\":\"{\\\"items\\\":[{\\\"id\\\":1,\\\"name\\\":\\\"widget\\\"},{\\\"id\\\":2,\\\"name\\\":\\\"gadget\\\"}]}\",\"redirects\":[]}"
   ],
   "s": {
    "nodes": {
     "api": "ok"
    },
    "stamps": {
     "api": "200 OK"
    },
    "verdict": [
     "A baked host answers. The body is in the record.",
     "ok"
    ]
   }
  },
  {
   "l": [
    "d",
    "$ fetch get /v1/items --host 127.0.0.1:18099"
   ]
  },
  {
   "l": [
    "b",
    "{\"rule\":\"fetch.host-not-allowed\",\"hint\":\"use one of detail.allowed\",\"detail\":{\"host\":\"127.0.0.1:18099\",\"allowed\":[\"127.0.0.1:18090\"]}}"
   ]
  },
  {
   "l": [
    "d",
    "# exit status 2"
   ],
   "s": {
    "nodes": {
     "other": "bad"
    },
    "stamps": {
     "other": "refused"
    },
    "verdict": [
     "A host outside the list is refused before a socket opens.",
     "ok"
    ]
   }
  },
  {
   "l": [
    "d",
    "$ fetch get /old --host 127.0.0.1:18090 --max-redirects 3"
   ]
  },
  {
   "l": [
    "b",
    "{\"rule\":\"fetch.redirect-not-allowed\",\"hint\":\"the redirect is not followed; fetch detail.location yourself if it is a host this build has, otherwise it is out of reach\",\"detail\":{\"location\":\"http://127.0.0.1:18092/v1/items\"}}"
   ]
  },
  {
   "l": [
    "d",
    "# exit status 2"
   ],
   "s": {
    "nodes": {
     "api": "ok",
     "evil": "bad"
    },
    "stamps": {
     "api": "302",
     "evil": "not followed"
    },
    "verdict": [
     "The server redirected somewhere not in the list. Not followed.",
     "ok"
    ]
   }
  },
  {
   "l": [
    "d",
    "$ fetch get /big --host 127.0.0.1:18090 --max-bytes 2000"
   ]
  },
  {
   "l": [
    "b",
    "{\"rule\":\"fetch.body-too-large\",\"hint\":\"raise --max-bytes, up to 16777216, or ask for less\",\"detail\":{\"max_bytes\":2000}}"
   ]
  },
  {
   "l": [
    "d",
    "# exit status 8"
   ],
   "s": {
    "nodes": {
     "api": "bad"
    },
    "stamps": {
     "api": "too large"
    },
    "verdict": [
     "The body is over the cap. It is refused, not cut off and passed on.",
     "ok"
    ]
   }
  },
  {
   "l": [
    "d",
    "$ echo '{\"name\":\"sprocket\"}' | fetch post /v1/items --host 127.0.0.1:18090 --body-stdin --content-type application/json"
   ]
  },
  {
   "l": [
    "g",
    "{\"method\":\"post\",\"scheme\":\"http\",\"host\":\"127.0.0.1:18090\",\"path\":\"/v1/items\",\"status\":201,\"body\":\"{\\\"created\\\":{\\\"name\\\":\\\"sprocket\\\"}}\"}"
   ],
   "s": {
    "nodes": {
     "api": "ok"
    },
    "stamps": {
     "api": "201 Created"
    },
    "verdict": [
     "A POST goes only where a GET may. The same row covers both.",
     "ok"
    ]
   }
  }
 ]
};
