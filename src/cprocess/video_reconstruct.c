/*
    This file is part of the Fluggo Media Library for high-quality
    video and audio processing.

    Copyright 2010 Brian J. Crowell <brian@fluggo.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <string.h>
#include "framework.h"

#undef G_LOG_DOMAIN
#define G_LOG_DOMAIN "fluggo.media.cprocess.video_reconstuct"

typedef struct {
    float cb, cr;
} cbcr_f32;

float
studio_chroma8_to_float( uint8_t chroma ) {
    return (chroma - 128.0f) / 224.0f;
}

float
studio_luma8_to_float( uint8_t luma ) {
    return (luma - 16.0f) / 219.0f;
}

/*
    Function: video_reconstruct_dv
    Reconstructs planar standard-definition NTSC DV:

    720x480 YCbCr
    4:1:1 subsampling, co-sited with left pixel
    Rec 709 matrix
    Rec 709 transfer function
*/
EXPORT void
video_reconstruct_dv( rgba_frame_f16 *frame, coded_image *planar ) {
    const int full_width = 720, full_height = 480;

    // Rec. 601 YCbCr->RGB matrix in Poynton, p. 305:
/*    const float colorMatrix[3][3] = {
        { 1.0f,  0.0f,       1.402f },
        { 1.0f, -0.344136f, -0.714136f },
        { 1.0f,  1.772f,     0.0f }
    };*/

    // Rec. 709 YCbCr->RGB matrix in Poynton, p. 316:
    const float colorMatrix[3][3] = {
        { 1.0f,  0.0f,       1.5748f },
        { 1.0f, -0.187324f, -0.468124f },
        { 1.0f,  1.8556f,    0.0f }
    };

    // Offset the frame so that line zero is part of the first field
    v2i picOffset = { 0, -1 };

    // Set up the current window
    box2i_set( &frame->current_window,
        max( picOffset.x, frame->full_window.min.x ),
        max( picOffset.y, frame->full_window.min.y ),
        min( full_width + picOffset.x - 1, frame->full_window.max.x ),
        min( full_height + picOffset.y - 1, frame->full_window.max.y ) );

    // Set up subsample support
    const int subX = 4;
    const float subOffsetX = 0.0f;

    // BJC: What follows is the horizontal-subsample-only case
    fir_filter triangleFilter = { NULL };
    filter_createTriangle( subX, subOffsetX, &triangleFilter );

    // Temp rows aligned to the AVFrame buffer [0, width)
    rgba_f32 *tempRow = g_slice_alloc( sizeof(rgba_f32) * full_width );
    cbcr_f32 *tempChroma = g_slice_alloc( sizeof(cbcr_f32) * full_width );

    // Turn into half RGB
    for( int row = frame->current_window.min.y - picOffset.y; row <= frame->current_window.max.y - picOffset.y; row++ ) {
        uint8_t *yrow = (uint8_t*) planar->data[0] + (row * planar->stride[0]);
        uint8_t *cbrow = (uint8_t*) planar->data[1] + (row * planar->stride[1]);
        uint8_t *crrow = (uint8_t*) planar->data[2] + (row * planar->stride[2]);

        memset( tempChroma, 0, sizeof(cbcr_f32) * full_width );

        int startx = 0, endx = (full_width - 1) / subX;

        for( int x = startx; x <= endx; x++ ) {
            float cb = studio_chroma8_to_float( cbrow[x] ), cr = studio_chroma8_to_float( crrow[x] );

            for( int i = max(frame->current_window.min.x - picOffset.x, x * subX - triangleFilter.center );
                    i <= min(frame->current_window.max.x - picOffset.x, x * subX + (triangleFilter.width - triangleFilter.center - 1)); i++ ) {

                tempChroma[i].cb += cb * triangleFilter.coeff[i - x * subX + triangleFilter.center];
                tempChroma[i].cr += cr * triangleFilter.coeff[i - x * subX + triangleFilter.center];
            }
        }

        for( int x = frame->current_window.min.x; x <= frame->current_window.max.x; x++ ) {
            float y = studio_luma8_to_float( yrow[x - picOffset.x] );

            tempRow[x].r = y * colorMatrix[0][0] +
                tempChroma[x - picOffset.x].cb * colorMatrix[0][1] +
                tempChroma[x - picOffset.x].cr * colorMatrix[0][2];
            tempRow[x].g = y * colorMatrix[1][0] +
                tempChroma[x - picOffset.x].cb * colorMatrix[1][1] +
                tempChroma[x - picOffset.x].cr * colorMatrix[1][2];
            tempRow[x].b = y * colorMatrix[2][0] +
                tempChroma[x - picOffset.x].cb * colorMatrix[2][1] +
                tempChroma[x - picOffset.x].cr * colorMatrix[2][2];
            tempRow[x].a = 1.0f;
        }

        rgba_f16 *out = video_get_pixel_f16( frame, frame->current_window.min.x, row + picOffset.y );

        rgba_f32_to_f16( out, tempRow + frame->current_window.min.x - picOffset.x,
            frame->current_window.max.x - frame->current_window.min.x + 1 );
        video_transfer_rec709_to_linear_scene( &out->r, &out->r,
            (sizeof(rgba_f16) / sizeof(half)) * (frame->current_window.max.x - frame->current_window.min.x + 1) );
    }

    filter_free( &triangleFilter );
    g_slice_free1( sizeof(rgba_f32) * full_width, tempRow );
    g_slice_free1( sizeof(cbcr_f32) * full_width, tempChroma );
}

static const char *recon_dv_shader_text =
"#version 110\n"
"#extension GL_ARB_texture_rectangle : enable\n"
"uniform sampler2DRect texY;"
"uniform sampler2DRect texCb;"
"uniform sampler2DRect texCr;"
"uniform vec2 picOffset;"
"uniform mat3 yuv2rgb;"

"void main() {"
"    vec2 yTexCoord = gl_TexCoord[0].st - picOffset;"
"    vec2 cTexCoord = (yTexCoord - vec2(0.5, 0.5)) * vec2(0.25, 1.0) + vec2(0.5, 0.5);"
"    float y = texture2DRect( texY, yTexCoord ).r - (16.0/256.0);"
"    float cb = texture2DRect( texCb, cTexCoord ).r - 0.5;"
"    float cr = texture2DRect( texCr, cTexCoord ).r - 0.5;"

"    vec3 ycbcr = vec3(y, cb, cr);"

"    gl_FragColor.rgb = pow(max(vec3(0.0), ycbcr * yuv2rgb), vec3(2.2));"
"    gl_FragColor.a = 1.0;"
"}";

typedef struct {
    GLhandleARB shader, program;
    int texY, texCb, texCr, yuv2rgb, picOffset;
} gl_shader_state;

static void destroy_shader( gl_shader_state *shader ) {
    // We assume that we're in the right GL context
    glDeleteObjectARB( shader->program );
    glDeleteObjectARB( shader->shader );
    g_free( shader );
}

EXPORT void
video_reconstruct_dv_gl( rgba_frame_gl *frame, coded_image *planar ) {
    GQuark shader_quark = g_quark_from_static_string( "cprocess::video_reconstruct::recon_dv_shader" );

    if( !planar ) {
        box2i_set_empty( &frame->current_window );
        return;
    }

    v2i frame_size;
    box2i_get_size( &frame->full_window, &frame_size );

    void *context = getCurrentGLContext();
    gl_shader_state *shader = (gl_shader_state *) g_dataset_id_get_data( context, shader_quark );

    if( !shader ) {
        // Time to create the program for this context
        shader = g_new0( gl_shader_state, 1 );

        g_debug( "Building DV shader..." );
        gl_buildShader( recon_dv_shader_text, &shader->shader, &shader->program );

        shader->texY = glGetUniformLocationARB( shader->program, "texY" );
        shader->texCb = glGetUniformLocationARB( shader->program, "texCb" );
        shader->texCr = glGetUniformLocationARB( shader->program, "texCr" );
        shader->yuv2rgb = glGetUniformLocationARB( shader->program, "yuv2rgb" );
        shader->picOffset = glGetUniformLocationARB( shader->program, "picOffset" );

        g_dataset_id_set_data_full( context, shader_quark, shader, (GDestroyNotify) destroy_shader );
    }

    GLuint textures[4];
    glGenTextures( 4, textures );

    // Set up the result texture
    glActiveTexture( GL_TEXTURE0 );
    glBindTexture( GL_TEXTURE_RECTANGLE_ARB, textures[3] );
    glTexImage2D( GL_TEXTURE_RECTANGLE_ARB, 0, GL_RGBA_FLOAT16_ATI, frame_size.x, frame_size.y, 0,
        GL_RGBA, GL_HALF_FLOAT_ARB, NULL );
    gl_checkError();

    // Offset the frame so that line zero is part of the first field
    // TODO: Should probably fold these constants into the shader
    v2i pic_offset = { 0, -1 };

    v2i y_size = { 720, 480 };
    v2i c_size = { 720 / 4, 480 };

    // Rec. 601 YCbCr->RGB matrix in Poynton, p. 305:
    // TODO: This should probably be configurable
    float color_matrix[3][3];
    color_matrix[0][0] = 1.0f;
    color_matrix[0][1] = 0.0f;
    color_matrix[0][2] = 1.402f;
    color_matrix[1][0] = 1.0f;
    color_matrix[1][1] = -0.344136f;
    color_matrix[1][2] = -0.714136f;
    color_matrix[2][0] = 1.0f;
    color_matrix[2][1] = 1.772f;
    color_matrix[2][2] = 0.0f;

    box2i_set( &frame->current_window,
        max( pic_offset.x, frame->full_window.min.x ),
        max( pic_offset.y, frame->full_window.min.y ),
        min( y_size.x + pic_offset.x - 1, frame->full_window.max.x ),
        min( y_size.y + pic_offset.y - 1, frame->full_window.max.y ) );

    // Set up the input textures
    glActiveTexture( GL_TEXTURE0 );
    glBindTexture( GL_TEXTURE_RECTANGLE_ARB, textures[0] );
    glPixelStorei( GL_UNPACK_ROW_LENGTH, planar->stride[0] );
    glTexImage2D( GL_TEXTURE_RECTANGLE_ARB, 0, GL_LUMINANCE8, y_size.x, y_size.y, 0,
        GL_LUMINANCE, GL_UNSIGNED_BYTE, planar->data[0] );
    glEnable( GL_TEXTURE_RECTANGLE_ARB );

    glActiveTexture( GL_TEXTURE1 );
    glBindTexture( GL_TEXTURE_RECTANGLE_ARB, textures[1] );
    glPixelStorei( GL_UNPACK_ROW_LENGTH, planar->stride[1] );
    glTexImage2D( GL_TEXTURE_RECTANGLE_ARB, 0, GL_LUMINANCE8, c_size.x, c_size.y, 0,
        GL_LUMINANCE, GL_UNSIGNED_BYTE, planar->data[1] );
    glEnable( GL_TEXTURE_RECTANGLE_ARB );

    glActiveTexture( GL_TEXTURE2 );
    glBindTexture( GL_TEXTURE_RECTANGLE_ARB, textures[2] );
    glPixelStorei( GL_UNPACK_ROW_LENGTH, planar->stride[2] );
    glTexImage2D( GL_TEXTURE_RECTANGLE_ARB, 0, GL_LUMINANCE8, c_size.x, c_size.y, 0,
        GL_LUMINANCE, GL_UNSIGNED_BYTE, planar->data[2] );
    glEnable( GL_TEXTURE_RECTANGLE_ARB );

    glPixelStorei( GL_UNPACK_ROW_LENGTH, 0 );

    glUseProgramObjectARB( shader->program );
    glUniform1iARB( shader->texY, 0 );
    glUniform1iARB( shader->texCb, 1 );
    glUniform1iARB( shader->texCr, 2 );
    glUniformMatrix3fvARB( shader->yuv2rgb, 1, false, &color_matrix[0][0] );
    glUniform2fARB( shader->picOffset, pic_offset.x, pic_offset.y );

    // The troops are ready; define the image
    frame->texture = textures[3];
    gl_renderToTexture( frame );

    glDeleteTextures( 3, textures );

    glUseProgramObjectARB( 0 );

    glActiveTexture( GL_TEXTURE2 );
    glDisable( GL_TEXTURE_RECTANGLE_ARB );
    glActiveTexture( GL_TEXTURE1 );
    glDisable( GL_TEXTURE_RECTANGLE_ARB );
    glActiveTexture( GL_TEXTURE0 );
    glDisable( GL_TEXTURE_RECTANGLE_ARB );
}

