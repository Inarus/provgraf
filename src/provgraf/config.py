from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql://provgraf:provgraf_local_dev@localhost:5632/provgraf"
    log_level: str = "INFO"

    # RAG / embeddings (local, mmlw)
    embedding_model: str = "sdadas/mmlw-retrieval-roberta-large"
    embedding_device: str = "mps"        # mps | cpu | cuda
    rag_min_similarity: float = 0.5      # cosine threshold for search/dedup (to be tuned)

    # Reranker (stage 2: a cross-encoder reranks the bi-encoder's top-N) — tuned as the project grows
    reranker_model: str = "sdadas/polish-reranker-large-ranknet"
    rerank_candidates: int = 20          # how many of mmlw's top-N go to the reranker
    rerank: bool = True                  # reranker enabled by default
