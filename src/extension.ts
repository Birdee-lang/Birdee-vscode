import * as path from 'path';
//import * as fs from 'fs';

import { workspace, ExtensionContext } from 'vscode';

import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
  TransportKind,
  ExecutableOptions,
  Executable,
  Trace
} from 'vscode-languageclient';

let client: LanguageClient;

export function activate(context: ExtensionContext) {

  // The server is implemented in another project and outputted there
  let serverCommand = context.asAbsolutePath(path.join('src', 'BirdeeLSP.py'));
  console.log(serverCommand)
  let commandOptions: ExecutableOptions = { stdio: 'pipe', detached: false };
  let serverOptions: Executable = {
    command: "d:\\Menooker\\CXX\\Birdee\\x64\\DynLLVM\\birdeec.exe",
    args: ["-s", "-i", serverCommand, "-o", "111.obj"],
    options: commandOptions
  };

  // Options of the language client
  let clientOptions: LanguageClientOptions = {
    // Activate the server for DOT files
    documentSelector: [{ scheme: 'file', language: 'Birdee' }],
    synchronize: {
      // Synchronize the section 'birdeeLanguageServer' of the settings to the server
      configurationSection: 'birdeeLanguageServer',
      // Notify the server about file changes to '.clientrc files contained in the workspace
      fileEvents: workspace.createFileSystemWatcher('**/.clientrc')
    },
  };
  console.log("AAAAA");
  // Create the language client and start the client.
  client = new LanguageClient('birdeeLanguageServer', 'Language Server', serverOptions, clientOptions)
  // Push the disposable to the context's subscriptions so that the 
  // client can be deactivated on extension deactivation

  client.trace = Trace.Verbose;
  client.start();
}

export function deactivate(): Thenable<void> {
  if (!client) {
    //return undefined;
  }
  return client.stop();
}