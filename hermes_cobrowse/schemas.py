"""OpenAI / Hermes compatible JSON schemas for CoBrowse tools."""

SCHEMAS = {
    "cobrowse_open": {
        "name": "cobrowse_open",
        "description": "Launch the CoBrowse co-browsing session, start the real-time visual streaming server for the localhost Spatial Canvas Studio, and navigate to an initial URL. Returns session ID and tokenized live preview URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Initial URL to navigate to. Defaults to 'https://google.com'.",
                },
                "viewport_width": {
                    "type": "integer",
                    "description": "Viewport width in pixels. Default is 1280.",
                },
                "viewport_height": {
                    "type": "integer",
                    "description": "Viewport height in pixels. Default is 800.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional browser window/session ID. Provide a distinct ID to run multiple browser windows concurrently on the Spatial Canvas.",
                },
                "new_window": {
                    "type": "boolean",
                    "description": "If True, opens a new independent browser window alongside existing windows.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional custom label for the browser window titlebar.",
                },
            },
        },
    },
    "cobrowse_navigate": {
        "name": "cobrowse_navigate",
        "description": "Navigate the co-browsing browser to a specified URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Target website URL (e.g. 'https://github.com/login').",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
            "required": ["url"],
        },
    },
    "cobrowse_get_state": {
        "name": "cobrowse_get_state",
        "description": "Extract the current browser page state: page title, URL, interactive elements (buttons, inputs, links with CSS selectors and coordinates), and whether human intervention is currently active.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_dom_tree": {
                    "type": "boolean",
                    "description": "Whether to return structured interactive elements. Default is True.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
        },
    },
    "cobrowse_click": {
        "name": "cobrowse_click",
        "description": "Click an element on the webpage by CSS selector, text match, or pixel coordinates (x, y). Triggers a visible animated Hermes cursor ripple in the live stream.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector or text match (e.g. 'button#submit', 'text=Sign In').",
                },
                "x": {
                    "type": "number",
                    "description": "Exact X pixel coordinate (optional if selector is given).",
                },
                "y": {
                    "type": "number",
                    "description": "Exact Y pixel coordinate (optional if selector is given).",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "middle", "right"],
                    "description": "Mouse button to click. Default is 'left'.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
        },
    },
    "cobrowse_type": {
        "name": "cobrowse_type",
        "description": "Type text into an input or active field. Displays real-time typing telemetry on the user's desktop canvas.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input field (e.g. 'input[name=\"email\"]'). Optional if element is already focused.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the element.",
                },
                "clear_first": {
                    "type": "boolean",
                    "description": "Whether to clear existing text in the input before typing. Defaults to True when a selector is given, False otherwise.",
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Whether to press Enter key after typing. Default is False.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
            "required": ["text"],
        },
    },
    "cobrowse_request_handoff": {
        "name": "cobrowse_request_handoff",
        "description": "PAUSE autonomous agent execution and request human intervention for sensitive operations (e.g., 2FA SMS code, CAPTCHA, bank authentication, payment checkout). Shows an alert in the Spatial Canvas Studio stream and awaits user action and confirmation.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why human intervention is required (e.g. '2FA code required from SMS', 'Cloudflare Turnstile challenge').",
                },
                "guidance": {
                    "type": "string",
                    "description": "Clear step-by-step instruction for the human user to perform on the embedded canvas.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds to wait for human completion before timing out. Default is 300.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
            "required": ["reason", "guidance"],
        },
    },
    "cobrowse_scroll": {
        "name": "cobrowse_scroll",
        "description": "Scroll the active web page in a specified direction.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom"],
                    "description": "Direction to scroll.",
                },
                "amount": {
                    "type": "integer",
                    "description": "Pixel amount to scroll (default 400). Ignored for 'top' and 'bottom'.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
            "required": ["direction"],
        },
    },
    "cobrowse_eval_js": {
        "name": "cobrowse_eval_js",
        "description": "Execute JavaScript directly in the browser page context and return the evaluated value.",
        "parameters": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "JavaScript code snippet to execute.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target window session ID (defaults to active session).",
                },
            },
            "required": ["script"],
        },
    },
    "cobrowse_close": {
        "name": "cobrowse_close",
        "description": "Gracefully close a specific browser window by session_id, or shut down all browser sessions and close the streaming server when omitted.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Optional browser session ID to close. If omitted, closes all sessions and shuts down the streaming server.",
                },
            },
        },
    },
}
