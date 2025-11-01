import os
from volcenginesdkarkruntime import Ark

client = Ark(base_url="https://ark.cn-beijing.volces.com/api/v3",api_key="165e659b-a12e-462d-8398-68da89fbcebb")

completion = client.chat.completions.create(
    model="doubao-1-5-pro-32k-250115",
    messages=[
        {"role": "system", "content": "你是人工智能助手."},
        {"role": "user", "content": "你好"},
    ],
)
print(completion.choices[0].message.content)