export type Book = { id:number; title:string; status:string; original_filename:string; created_at:string; patches?:{total:number;done:number;active:number;failed:number} };
export type Patch = { id:number; patch_index:number; name:string; status:string; chunk_count:number; next_chunk_index:number; error_message:string|null };
export type Chapter = { id:number; chapter_index:number; title:string; char_count:number; is_excluded:boolean };
export type Job = { id:number; job_type:string; status:string; phase:string; percent:number; book_id:number|null; error_message:string|null; created_at:string };
export type Media = { music:Array<{id:number;name:string;duration_sec:number|null}>; photos:Array<{name:string;size:number;is_video:boolean}>; voices:Array<{name:string;size:number;description:string}> };

export async function api<T>(url:string, init?:RequestInit):Promise<T>{
  const response=await fetch(url, init);
  if(!response.ok){let message=`Lỗi ${response.status}`;try{const body=await response.json();message=body.detail||message}catch{}throw new Error(message)}
  const type=response.headers.get("content-type")||"";
  return (type.includes("json")?await response.json():await response.text()) as T;
}
export const post=(url:string, body?:BodyInit)=>api(url,{method:"POST",body});
