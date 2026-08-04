import { randomUUID } from "node:crypto";
import type { TSchema } from "typebox";
import { Check } from "typebox/value";

import {
  ActResponseSchema,
  QueryResponseSchema,
  RunResponseSchema,
  SEMANTIC_PROTOCOL_VERSION,
  SemanticValidationError,
  VerifyResponseSchema,
  validateSemanticRequest,
  validateSemanticResponse,
  type ActPayload,
  type ActResult,
  type QueryPayload,
  type QueryResult,
  type RunPayload,
  type RunResult,
  type SemanticError,
  type SemanticRequest,
  type VerifyPayload,
  type VerifyResult,
} from "./protocol.js";

export interface SemanticTransport {
  send(request: SemanticRequest): Promise<unknown>;
}

export class SemanticRemoteError extends Error {
  readonly semanticError: SemanticError;
  constructor(error: SemanticError) {
    super(error.message);
    this.name = "SemanticRemoteError";
    this.semanticError = error;
  }
}

export interface SemanticClientOptions {
  episodeId: string;
  requestId?: () => string;
}

type Operation = SemanticRequest["operation"];

/** Attaches harness-owned episode identity and validates both transport directions. */
export class SemanticComputerClient {
  readonly #transport: SemanticTransport;
  readonly #episodeId: string;
  readonly #requestId: () => string;

  constructor(transport: SemanticTransport, options: SemanticClientOptions) {
    this.#transport = transport;
    this.#episodeId = options.episodeId;
    this.#requestId = options.requestId ?? randomUUID;
  }

  query(payload: QueryPayload): Promise<QueryResult> {
    return this.#dispatch("query", payload, QueryResponseSchema);
  }

  act(payload: ActPayload): Promise<ActResult> {
    return this.#dispatch("act", payload, ActResponseSchema);
  }

  verify(payload: VerifyPayload): Promise<VerifyResult> {
    return this.#dispatch("verify", payload, VerifyResponseSchema);
  }

  run(payload: RunPayload): Promise<RunResult> {
    return this.#dispatch("run", payload, RunResponseSchema);
  }

  async #dispatch<R>(operation: Operation, payload: unknown, responseSchema: TSchema): Promise<R> {
    const request = validateSemanticRequest({
      protocol_version: SEMANTIC_PROTOCOL_VERSION,
      request_id: this.#requestId(),
      episode_id: this.#episodeId,
      operation,
      payload,
    });
    const response = validateSemanticResponse(await this.#transport.send(request));
    if (response.request_id !== request.request_id) {
      throw new SemanticValidationError(
        `Semantic response request_id mismatch: expected ${request.request_id}, received ${response.request_id}`,
      );
    }
    if (!Check(responseSchema, response)) {
      throw new SemanticValidationError(`Result does not match ${operation} response schema`);
    }
    if (response.status !== "ok" && response.error !== null) {
      throw new SemanticRemoteError(response.error);
    }
    if (response.result === null) {
      throw new SemanticValidationError(`${operation} response did not contain a result`);
    }
    return response.result as R;
  }
}
