"""
Example custom LLM adapter.

Steps to use:
  1. Copy this file to your project
  2. Implement the chat() method to call your LLM
  3. Configure in gateway.yaml:
       adapters:
         - name: my-llm
           type: plugin
           module: "my_adapters.my_llm.MyLLMAdapter"
"""
import os
import httpx
from gateway.adapters.base import BaseLLMAdapter, ChatRequest, ChatResponse


class MyLLMAdapter(BaseLLMAdapter):
    """
    Replace this class with your own implementation.
    The only required method is chat().
    """

    name = "my-llm"

    def __init__(self):
        super().__init__()
        # Initialize: load config, create HTTP client, etc.
        self._base_url = "https://your-llm-api.internal/v1"
        self._api_key = os.environ.get("MY_LLM_API_KEY", "")  # set MY_LLM_API_KEY in your environment

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """
        Send request to LLM and return ChatResponse.
        Gateway calls this method for each incoming request.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/chat",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": request.model,
                    "messages": [
                        {"role": m.role, "content": m.content}
                        for m in request.messages
                    ],
                },
            )
            response.raise_for_status()
            data = response.json()

        # Adapt your LLM API response structure to ChatResponse
        return ChatResponse(
            content=data["response"]["text"],   # replace with real path in your response
            model=request.model,
            usage={
                "prompt_tokens": data.get("usage", {}).get("input", 0),
                "completion_tokens": data.get("usage", {}).get("output", 0),
                "total_tokens": data.get("usage", {}).get("total", 0),
            },
        )
