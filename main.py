import os
os.environ['OPENAI_API_KEY'] = 'YOUR_OPENAI_API_KEY'
import pickle

from google.auth.transport.requests import Request

from google_auth_oauthlib.flow import InstalledAppFlow
from llama_index import GPTVectorStoreIndex, download_loader




def authorize_gdocs():
    google_oauth2_scopes = [
        "https://www.googleapis.com/auth/documents.readonly"
    ]
    cred = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", 'rb') as token:
            cred = pickle.load(token)
    if not cred or not cred.valid:
        if cred and cred.expired and cred.refresh_token:
            cred.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", google_oauth2_scopes)
            cred = flow.run_local_server(port=0)
        with open("token.pickle", 'wb') as token:
            pickle.dump(cred, token)


if __name__ == '__main__':

    authorize_gdocs()
    GoogleDocsReader = download_loader('GoogleDocsReader')
    gdoc_ids = ['1iYohwxQWoHcjjVXyGtKoYkoqHO29-xnkFomvzoil2nk','150Xd1HyOSa2RyqEa6i08aF-GB8IvgwNyyQQa5yGDhUw']
    loader = GoogleDocsReader()
    documents = loader.load_data(document_ids=gdoc_ids)
    index = GPTVectorStoreIndex.from_documents(documents)

    while True:
        prompt = input("Type prompt...")+' include the document ID where this is from in the format of a google doc link like https://docs.google.com/document/d/[documentid] '
        query_engine = index.as_query_engine()
        response = query_engine.query(prompt)
        print(response)

       
